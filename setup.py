import sys
import hashlib
from pathlib import Path

from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup


class Build(build_ext):
    def run(self):
        super().run()
        binary = Path(self.get_ext_fullpath("step2torque._assembly"))
        binary.with_name("_assembly_version.py").write_text(
            "SHA256 = " + repr(hashlib.sha256(binary.read_bytes()).hexdigest()) + "\n"
        )


threads = [] if sys.platform == "win32" else ["-pthread"]
optimize = ["/O2"] if sys.platform == "win32" else ["-O3"]
setup(
    ext_modules=[
        Pybind11Extension(
            "step2torque._assembly",
            ["src/step2torque/_assembly.cpp"],
            cxx_std=17,
            define_macros=[("MOTOR_THREADS", "1")],
            extra_compile_args=threads + optimize,
            extra_link_args=threads,
        )
    ],
    cmdclass={"build_ext": Build},
)
