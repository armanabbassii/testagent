"""
src/task_sources/jira/transport.py — یک درخواستِ HTTP با timeout و تأییدِ گواهی

خطاهای شبکه/SSLِ requests به JiraConnectionError با پیامِ قابلِ اقدام تبدیل
می‌شوند. تأییدِ گواهی همیشه روشن است (JIRA_CA_BUNDLE یا certifi).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import requests

from src.task_sources.jira.errors import JiraConnectionError
from src.task_sources.jira.settings import JiraSettings


def send(
    session: requests.Session, method: str, url: str, settings: JiraSettings, **kwargs: Any
) -> requests.Response:
    kwargs.setdefault("timeout", settings.timeout)
    host = urlsplit(url).netloc
    try:
        return session.request(method, url, verify=settings.verify, **kwargs)
    except requests.exceptions.SSLError as exc:
        hint = (
            f" The configured JIRA_CA_BUNDLE ({settings.ca_bundle}) does not verify it."
            if settings.ca_bundle
            else " If the server sends an incomplete certificate chain, set JIRA_CA_BUNDLE "
            "to a PEM file with the missing intermediate certificate."
        )
        raise JiraConnectionError(
            f"The TLS certificate of {host} could not be verified.{hint}"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise JiraConnectionError(f"{host} did not answer in time ({type(exc).__name__}).") from exc
    except requests.exceptions.RequestException as exc:
        raise JiraConnectionError(f"Could not connect to {host} ({type(exc).__name__}).") from exc


__all__ = ["send"]
