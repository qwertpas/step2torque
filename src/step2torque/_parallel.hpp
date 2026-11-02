#pragma once
#include <algorithm>
#include <functional>
#include <memory>
#include <vector>
#ifdef MOTOR_THREADS
#include <condition_variable>
#include <mutex>
#include <thread>
#endif

// A small, persistent pool. All callers wait before reusing their input arrays.
class Parallel {
    int size_=1;
#ifdef MOTOR_THREADS
    std::vector<std::thread> workers;
    std::mutex mutex;
    std::condition_variable start,finish;
    std::function<void(int,int,int)> job;
    int generation=0,completed=0,length=0;
    bool stopped=false;
#endif
public:
    explicit Parallel(int count=1) {
#ifdef MOTOR_THREADS
        size_=std::max(1,count);
        for(int id=1;id<size_;++id) workers.emplace_back([this,id]{
            int seen=0;
            for(;;) {
                std::unique_lock<std::mutex> lock(mutex);
                start.wait(lock,[&]{return stopped || generation!=seen;});
                if(stopped)return;
                seen=generation;int n=length;
                lock.unlock();job(n*id/size_,n*(id+1)/size_,id);
                lock.lock();++completed;finish.notify_one();
            }
        });
#endif
    }
    ~Parallel() {
#ifdef MOTOR_THREADS
        {std::lock_guard<std::mutex> lock(mutex);stopped=true;}
        start.notify_all();for(auto &worker:workers)worker.join();
#endif
    }
    int size()const{return size_;}
    template<class Function> void run(int n,Function function) {
#ifdef MOTOR_THREADS
        if(size_>1 && n>=1024) {
            {std::lock_guard<std::mutex> lock(mutex);job=function;length=n;completed=0;++generation;}
            start.notify_all();function(0,n/size_,0);
            std::unique_lock<std::mutex> lock(mutex);finish.wait(lock,[&]{return completed==size_-1;});
            return;
        }
#endif
        function(0,n,0);
    }
};

