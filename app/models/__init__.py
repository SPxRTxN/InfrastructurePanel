"""Импорт всех моделей, чтобы Base.metadata (и Alembic) видели все таблицы."""
from app.models.agent_releases import AgentRelease, NodeUpdate  # noqa: F401
from app.models.agent_install import AgentInstall  # noqa: F401
from app.models.alerts import AlertLog, AlertRule, AlertState  # noqa: F401
from app.models.archive import Archive, SecurityEvent  # noqa: F401
from app.models.auth import Group, GroupPermission, LoginAttempt, User, UserSession, user_groups  # noqa: F401
from app.models.backups import BackupLog  # noqa: F401
from app.models.calendar import Event, ReminderLog  # noqa: F401
from app.models.jira import Board, BoardColumn, Issue  # noqa: F401
from app.models.confluence import Page, PageVersion, Space, issue_pages  # noqa: F401
from app.models.maintenance import Maintenance  # noqa: F401
from app.models.nodes import Node, NodeAction, NodeEvent, NodeSample, NodeTombstone  # noqa: F401
from app.models.notification import Notification  # noqa: F401
from app.models.recordings import ConsoleRecording  # noqa: F401
from app.models.servers import Server, ServerCredential, ServerMetric, Service, SshHostKey  # noqa: F401
from app.models.settings import SettingValue  # noqa: F401
from app.models.topology import Device, Link, Segment  # noqa: F401
from app.models.vpn import VpnAssignment, VpnSubscription  # noqa: F401
