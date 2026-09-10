import re
from typing import Any, Dict, List, Optional


def validate_required_fields(data: Dict[str, Any], required_fields: List[str]) -> Optional[str]:
    """Checks that all required fields are present in data."""
    missing = [f for f in required_fields if f not in data or data[f] is None or data[f] == ""]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"
    return None


def validate_email(email: str) -> bool:
    """Basic email regex validation."""
    regex = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    return bool(re.match(regex, email))


def validate_phone(phone: str) -> bool:
    """Phone number validation."""
    cleaned = re.sub(r"[\s\-\+\(\)]", "", phone)
    return len(cleaned) >= 10
