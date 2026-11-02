// Same affine P2, degree-6 iron quadrature and B(H) energy as _field.py.
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include "_parallel.hpp"
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>
namespace py = pybind11;
using Real = py::array_t<double, py::array::c_style>;
using Int = py::array_t<int, py::array::c_style | py::array::forcecast>;
using Index = py::array_t<int64_t, py::array::c_style | py::array::forcecast>;
struct Curve {
    std::vector<double> h,b,slope,area;
    std::array<double,3> evaluate(double value) const {
        int i=std::upper_bound(h.begin(),h.end(),value)-h.begin()-1;
        i=std::max(0,std::min(i,int(h.size())-2));
        double t=value-h[i];
        return {b[i]+t*slope[i],slope[i],area[i]+t*(b[i]+.5*t*slope[i])};
    }
};
class Assembly {
    int n,ne,nq;
    int64_t nnz;
    std::vector<int64_t> ptr,entries,cell_ptr,cell_entries;
    std::vector<int> col,dofs,curve;
    std::vector<double> inverse,volume,reference,weights,points,source;
    std::vector<double> local_matrix,local_load,local_energy;
    std::vector<Curve> curves;
    Parallel pool;
    const double *potential=nullptr;
    double *matrix=nullptr,*residual=nullptr;
    double factor=1,floor=.1;
public:
    Assembly(Index pointers,Int columns,Int indices,Int labels,Real jac,
             Real volumes,Real shapes,Real quadrature,Real locations,
             py::list laws,int threads)
      : n(pointers.size()-1),ne(labels.size()),nq(quadrature.size()),nnz(columns.size()),
        ptr(pointers.data(),pointers.data()+pointers.size()),
        col(columns.data(),columns.data()+columns.size()),
        dofs(indices.data(),indices.data()+indices.size()),
        curve(labels.data(),labels.data()+labels.size()),
        inverse(jac.data(),jac.data()+jac.size()),
        volume(volumes.data(),volumes.data()+volumes.size()),
        reference(shapes.data(),shapes.data()+shapes.size()),
        weights(quadrature.data(),quadrature.data()+quadrature.size()),
        points(locations.data(),locations.data()+locations.size()),
        source(size_t(ne)*12),local_matrix(size_t(ne)*55),local_load(size_t(ne)*10),
        local_energy(ne),pool(threads) {
        if(indices.size()!=int64_t(ne)*10 || shapes.size()!=nq*30 || jac.size()!=int64_t(ne)*9)
            throw std::runtime_error("Compiled assembly requires the affine P2 tetrahedron map");
        for(auto record:laws) {
            py::tuple pair=py::cast<py::tuple>(record);
            Real h=py::cast<Real>(pair[0]),b=py::cast<Real>(pair[1]);
            Curve c;
            c.h.assign(h.data(),h.data()+h.size());c.b.assign(b.data(),b.data()+b.size());
            c.slope.resize(h.size()-1);c.area.assign(h.size(),0);
            for(int i=0;i<h.size()-1;++i) {
                c.slope[i]=(c.b[i+1]-c.b[i])/(c.h[i+1]-c.h[i]);
                c.area[i+1]=c.area[i]+.5*(c.b[i]+c.b[i+1])*(c.h[i+1]-c.h[i]);
            }
            curves.push_back(std::move(c));
        }
        entries.resize(size_t(ne)*100);cell_ptr.assign(n+1,0);cell_entries.resize(size_t(ne)*10);
        for(int row:dofs) {
            if(row<0 || row>=n)throw std::runtime_error("Invalid P2 degree of freedom");
            ++cell_ptr[row+1];
        }
        for(int i=1;i<=n;++i)cell_ptr[i]+=cell_ptr[i-1];
        auto cursor=cell_ptr;
        for(int64_t i=0;i<int64_t(ne)*10;++i)cell_entries[cursor[dofs[i]]++]=i;
        for(int e=0;e<ne;++e)for(int i=0;i<10;++i)for(int j=0;j<10;++j) {
            int row=dofs[e*10+i],column=dofs[e*10+j];
            auto it=std::lower_bound(col.begin()+ptr[row],col.begin()+ptr[row+1],column);
            if(it==col.begin()+ptr[row+1] || *it!=column)
                throw std::runtime_error("Tangent graph differs from finite element map");
            entries[int64_t(e)*100+i*10+j]=it-col.begin();
        }
    }
    void set_source(Real values) {
        if(values.size()!=int64_t(ne)*12)throw std::runtime_error("Winding source differs from mesh");
        std::copy(values.data(),values.data()+values.size(),source.begin());
    }
    void assemble(Real phi,double scale,double lower,Real values,Real load) {
        potential=phi.data();factor=scale;floor=lower;
        if(phi.size()!=n || values.size()!=nnz || load.size()!=n)
            throw std::runtime_error("Tangent arrays differ from finite element map");
        matrix=values.mutable_data();residual=load.mutable_data();
        py::gil_scoped_release release;
        integrate(true);
    }
    double energy(Real phi,double scale) {
        potential=phi.data();factor=scale;
        py::gil_scoped_release release;
        return integrate(false);
    }
private:
    double integrate(bool assemble) {
        if(assemble) {std::fill(matrix,matrix+nnz,0);std::fill(residual,residual+n,0);}
        pool.run(ne,[&](int begin,int end,int){for(int e=begin;e<end;++e) {
            const double *jac=inverse.data()+9*e,*hs=source.data()+12*e;
            const int *ids=dofs.data()+10*e;
            const Curve &law=curves[curve[e]];
            double local[55]={},load[10]={},cell_energy=0;
            double offset=law.evaluate(.0001)[2];
            for(int q=0;q<nq;++q) {
                const double *ref=reference.data()+30*q,*point=points.data()+3*q;
                double gradient[10][3],h[3],s[3];
                for(int c=0;c<3;++c) {
                    s[c]=factor*(hs[c]+point[0]*(hs[3+c]-hs[c])+point[1]*(hs[6+c]-hs[c])+point[2]*(hs[9+c]-hs[c]));
                    h[c]=s[c];
                }
                for(int i=0;i<10;++i) for(int c=0;c<3;++c) {
                    gradient[i][c]=ref[3*i]*jac[c]+ref[3*i+1]*jac[3+c]+ref[3*i+2]*jac[6+c];
                    h[c]-=potential[ids[i]]*gradient[i][c];
                }
                double norm2=h[0]*h[0]+h[1]*h[1]+h[2]*h[2]+1e-8;
                double hn=std::sqrt(norm2),w=volume[e]*weights[q];
                auto bh=law.evaluate(hn);
                constexpr double mu0=1.2566370614359173e-6;
                cell_energy+=w*(bh[2]-offset-.5*mu0*(s[0]*s[0]+s[1]*s[1]+s[2]*s[2]));
                if(!assemble) continue;
                double secant=bh[0]/hn;
                double anisotropic=(std::max(bh[1],floor*secant)-secant)/norm2;
                double hg[10];
                for(int i=0;i<10;++i) {
                    hg[i]=h[0]*gradient[i][0]+h[1]*gradient[i][1]+h[2]*gradient[i][2];
                    load[i]+=w*secant*hg[i];
                }
                for(int i=0;i<10;++i) for(int j=0;j<=i;++j) {
                    double gram=gradient[i][0]*gradient[j][0]+gradient[i][1]*gradient[j][1]+gradient[i][2]*gradient[j][2];
                    local[i*(i+1)/2+j]+=w*(secant*gram+anisotropic*hg[i]*hg[j]);
                }
            }
            local_energy[e]=cell_energy;
            if(assemble) {
                std::copy(local,local+55,local_matrix.begin()+e*55);
                std::copy(load,load+10,local_load.begin()+e*10);
            }
        }});
        if(assemble)pool.run(n,[&](int begin,int end,int){for(int row=begin;row<end;++row) {
            for(int64_t k=cell_ptr[row];k<cell_ptr[row+1];++k) {
                int64_t entry=cell_entries[k];int e=entry/10,i=entry%10;
                residual[row]+=local_load[entry];
                for(int j=0;j<10;++j) {
                    int a=std::max(i,j),b=std::min(i,j);
                    matrix[entries[int64_t(e)*100+i*10+j]]+=local_matrix[e*55+a*(a+1)/2+b];
                }
            }
        }});
        double energy=0;for(double value:local_energy)energy+=value;
        return energy;
    }
};
PYBIND11_MODULE(_assembly,m) {
    py::class_<Assembly>(m,"Assembly")
        .def(py::init<Index,Int,Int,Int,Real,Real,Real,Real,Real,py::list,int>())
        .def("set_source",&Assembly::set_source)
        .def("assemble",&Assembly::assemble)
        .def("energy",&Assembly::energy);
}
