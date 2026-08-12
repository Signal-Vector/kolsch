"""Public widget surface for the Textual chat UI.

Re-exports every widget class so callers can do
``from cli.chat.widgets import UserChatbox`` instead of importing
each widget module individually. Per the spec's Design Principles
rule 2 (modular), each widget lives in its own file with its own
``DEFAULT_CSS`` and its own Pilot test file; this package
``__init__`` is the assembled public surface.
"""

from __future__ import annotations

from cli.chat.widgets.approval_block import ApprovalBlock
from cli.chat.widgets.approval_choice_pane import ApprovalChoicePane
from cli.chat.widgets.approval_from_edit import ApprovalFromEdit
from cli.chat.widgets.approval_pattern_edit import ApprovalPatternEdit
from cli.chat.widgets.approval_save_confirm import ApprovalSaveConfirm
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.composer import Composer
from cli.chat.widgets.force_kill_confirm import ForceKillConfirm
from cli.chat.widgets.info_bar import InfoBar
from cli.chat.widgets.scrollback_cue import ScrollbackCue
from cli.chat.widgets.slash_popup import SlashPopup
from cli.chat.widgets.status_bar import StatusBar
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

__all__ = [
    "ApprovalBlock",
    "ApprovalChoicePane",
    "ApprovalFromEdit",
    "ApprovalPatternEdit",
    "ApprovalSaveConfirm",
    "AssistantProse",
    "Composer",
    "ForceKillConfirm",
    "InfoBar",
    "ScrollbackCue",
    "SlashPopup",
    "StatusBar",
    "ToolCallBlock",
    "UserChatbox",
    "WorkflowNodeRow",
]
