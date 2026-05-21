"""Tool definitions for Claude function calling — assembled by domain."""

from ai.tools.crm import CRM_TOOLS
from ai.tools.finance import FINANCE_TOOLS
from ai.tools.marketing import MARKETING_TOOLS


def get_tools_definitions():
    """Full list of tool definitions for Claude."""
    return CRM_TOOLS + MARKETING_TOOLS + FINANCE_TOOLS
