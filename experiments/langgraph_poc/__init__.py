"""LangGraph proof of concept for the WorkplaceOps workflow (experiment only, see README.md)."""
__all__ = ["build_graph", "resume", "run_graph", "start"]


def __getattr__(name):  # lazy, so `python -m experiments.langgraph_poc.graph` does not double-import
    if name in __all__:
        from experiments.langgraph_poc import graph
        return getattr(graph, name)
    raise AttributeError(name)
