"""Compatibility shim: `streamlit run ui/streamlit_app.py` runs the role-based portal in ui/app.py.

Preferred entry point:  python -m streamlit run ui/app.py
"""
import pathlib
import runpy

runpy.run_path(str(pathlib.Path(__file__).resolve().with_name("app.py")), run_name="__main__")
