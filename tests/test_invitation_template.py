"""Invitation branding and Cognito substitutions stay consistent on deployment."""
from pathlib import Path

import yaml


def test_invitation_has_consistent_brand_and_encoding_safe_subject():
    template = yaml.load(
        (Path(__file__).resolve().parents[1] / "cognito-template.yaml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    invite = template["Resources"]["KineticUserPool"]["Properties"]["AdminCreateUserConfig"]["InviteMessageTemplate"]
    assert invite["EmailSubject"] == "Welcome to Kenetic ERP - Your account invitation"
    assert invite["EmailSubject"].isascii()
    assert "Welcome to Kenetic ERP!" in invite["EmailMessage"]
    assert "Kinetic" not in invite["EmailMessage"]
    assert invite["EmailMessage"].isascii()
    assert invite["EmailMessage"].count("{username}") == 1
    assert invite["EmailMessage"].count("{####}") == 1
    assert "choose a new password" in invite["EmailMessage"]
