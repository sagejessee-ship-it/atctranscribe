"""Corpus semantics shared by the control plane and workers.

Everything in this package is pure: it never touches the database or the
filesystem. Workers observe files; the control plane interprets observations
using the functions defined here.
"""
