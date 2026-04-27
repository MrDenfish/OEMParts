"""Fitment filter builder for eBay compatibility_filter parameter.

Constructs the filter string used by the Browse API to narrow results
to parts that actually fit a specific vehicle. Only works within eBay
Motors categories (6028 + descendants) — silently ignored elsewhere.
"""


def build_compatibility_filter(year: int, make: str, model: str) -> str:
    """Build eBay compatibility_filter string from vehicle Year/Make/Model.

    Returns a string like 'Year:2012,Make:Land Rover,Model:LR4'
    which is passed as the compatibility_filter parameter to the Browse API.
    """
    return f"Year:{year},Make:{make},Model:{model}"
