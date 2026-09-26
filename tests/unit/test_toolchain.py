"""M0 smoke test: the interpreter and the installed package are the ones we pinned."""

import sys

import gateway


def test_python_is_final_314() -> None:
    assert sys.version_info[:2] == (3, 14)
    assert sys.version_info.releaselevel == "final"  # the VM's old uv once gave us 3.14.0rc2


def test_template_strings_available() -> None:
    # M4 builds SOAP envelopes with t-strings (PEP 750), new in 3.14.
    from string.templatelib import Interpolation, Template

    value = "<x/>"
    tpl = t"<a>{value}</a>"
    assert isinstance(tpl, Template)
    assert [type(p) for p in tpl] == [str, Interpolation, str]


def test_gateway_package_importable() -> None:
    assert gateway.__version__ == "0.1.0"
