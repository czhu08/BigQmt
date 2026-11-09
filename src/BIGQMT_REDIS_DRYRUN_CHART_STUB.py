#coding:gbk
"""Inert placeholder for the retired multi-file bridge entry.

This QMT terminal forbids strategy FileIO (``PermissionError: Forbidden
FileIO``), so ``BIGQMT_REDIS_DRYRUN.py`` can never load its colocated modules
here. The real bridge runs from ``BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py`` under
model trading.

The old filename is still referenced by a K-line chart indicator on
SZ002714. Deploying this stub there keeps that chart from raising a traceback
on every reload, while the working bridge is untouched.
"""

_BANNER = (
    "[bigqmt] BIGQMT_REDIS_DRYRUN.py is retired on this terminal: the QMT "
    "sandbox forbids strategy FileIO. The RPC bridge runs from "
    "BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py under model trading; remove this "
    "indicator from the chart."
)


def init(*args, **kwargs):
    print(_BANNER)


def after_init(*args, **kwargs):
    return None


def handlebar(*args, **kwargs):
    return None


def adjust(*args, **kwargs):
    return None


def stop(*args, **kwargs):
    return None
