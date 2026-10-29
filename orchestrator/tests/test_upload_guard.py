import pytest
from neo4j.exceptions import Neo4jError

from bluegreen.cycle_aura import _upload_guard
from bluegreen.errors import VerificationError


class _Strategy:
    def __init__(self, target=None, error=None):
        self.target = target
        self.error = error

    def current_target(self, tenant):
        if self.error:
            raise self.error
        return self.target


def test_refuses_when_alias_lookup_fails():
    with pytest.raises(VerificationError, match="could not read alias target"):
        _upload_guard(_Strategy(error=Neo4jError("unavailable")), "acme", "acme-c0002")


def test_refuses_upload_into_the_live_database():
    with pytest.raises(VerificationError, match="already points at"):
        _upload_guard(_Strategy(target="acme-c0002"), "acme", "acme-c0002")


def test_allows_upload_into_a_new_database():
    _upload_guard(_Strategy(target="acme-c0001"), "acme", "acme-c0002")
    _upload_guard(_Strategy(target=None), "acme", "acme-c0001")
