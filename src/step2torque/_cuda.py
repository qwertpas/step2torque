"""Stateful cuDSS Cholesky for real NGSolve matrices with fixed sparsity."""

import importlib.metadata as metadata
import logging
import time
import weakref

import numpy as np
import scipy.sparse as sp


class CuDSS:
    def __init__(self, matrix, free_dofs):
        import nvmath.sparse.advanced as sparse

        self.matrix = matrix
        self.free = np.flatnonzero(np.asarray(free_dofs, dtype=bool))
        self.rhs = np.zeros(len(self.free))
        self.solver = None
        self.sparse = sparse
        self.stats = dict(
            export_s=0.0,
            setup_s=0.0,
            factor_s=0.0,
            solve_s=0.0,
            factorizations=0,
            solves=0,
        )
        self.Update()

    def Update(self):
        started = time.perf_counter()
        values, columns, pointers = (np.asarray(x) for x in self.matrix.CSR())
        if self.solver is None:
            rows = np.repeat(
                np.arange(self.matrix.height), np.diff(pointers).astype(np.int64)
            )
            mapping = np.full(self.matrix.height, -1, dtype=np.int32)
            mapping[self.free] = np.arange(len(self.free), dtype=np.int32)
            active = (mapping[rows] >= 0) & (mapping[columns] >= 0)
            if np.any(active & (rows > columns)) and not np.any(
                active & (rows < columns)
            ):
                raise ValueError("NGSolve CSR must expose the full symmetric matrix.")
            self.entries = np.flatnonzero(active & (rows <= columns))
            r, c = mapping[rows[self.entries]], mapping[columns[self.entries]]
            assert np.count_nonzero(r == c) == len(self.free)
            order = np.lexsort((c, r))
            self.entries = self.entries[order]
            indptr = np.r_[
                0, np.cumsum(np.bincount(r, minlength=len(self.free)), dtype=np.int64)
            ]
            assert indptr[-1] < 2**31
            self.upper = sp.csr_matrix(
                (values[self.entries].copy(), c[order], indptr.astype(np.int32)),
                shape=(len(self.free), len(self.free)),
            )
        else:
            # CSR values share NGSolve storage; the sparsity map stays fixed.
            self.upper.data[:] = values[self.entries]
        self.stats["last_export_s"] = time.perf_counter() - started
        self.stats["export_s"] += self.stats["last_export_s"]
        if self.solver is None:
            library = metadata.distribution("nvidia-cudss-cu12")
            threading = [
                library.locate_file(path)
                for path in library.files
                if str(path).endswith("libcudss_mtlayer_gomp.so.0")
            ]
            logger = logging.Logger("cudss", level=logging.ERROR)
            options = dict(
                sparse_system_type=self.sparse.DirectSolverMatrixType.SPD,
                sparse_system_view=self.sparse.DirectSolverMatrixViewType.UPPER,
                blocking=True,
                logger=logger,
            )
            if threading:
                options["multithreading_lib"] = str(threading[0])
            started = time.perf_counter()
            self.solver = self.sparse.DirectSolver(
                self.upper, self.rhs, options=options, execution="cuda"
            )
            # The native object requires explicit cleanup, including after the last pose.
            self.cleanup = weakref.finalize(self, self.solver.free)
            self.solver.plan()
            self.stats["setup_s"] += time.perf_counter() - started
        else:
            self.solver.reset_operands(a=self.upper)
        started = time.perf_counter()
        self.solver.factorize()
        self.stats["factor_s"] += time.perf_counter() - started
        self.stats["factorizations"] += 1

    def __mul__(self, rhs):
        started = time.perf_counter()
        self.rhs[:] = rhs.FV().NumPy()[self.free]
        self.solver.reset_operands(b=self.rhs)
        values = self.solver.solve()
        if not np.isfinite(values).all():
            raise RuntimeError("CUDA linear solve returned a non-finite field")
        answer = self.matrix.CreateColVector()
        array = answer.FV().NumPy()
        array[:] = 0
        array[self.free] = values
        self.stats["solve_s"] += time.perf_counter() - started
        self.stats["solves"] += 1
        return answer

    def close(self):
        self.cleanup()
