from PyInstaller.utils.hooks import collect_delvewheel_libs_directory

# The Windows wheel puts libjxl and its dependencies beside the Python package.
datas, binaries = collect_delvewheel_libs_directory("pillow_jxl", "pillow_jxl_plugin.libs")
