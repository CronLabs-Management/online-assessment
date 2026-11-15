import re
import unicodedata

from django.core.exceptions import ValidationError


def validate_phone_number(value):
    """
    Validates mobile and telephone numbers:
    - Rejects strings containing letters or words.
    - Allows optional leading '+', digits, and common separators (spaces, hyphens, dots, parentheses).
    - Requires between 7 and 15 digits (ITU-T E.164 international standard).
    - Rejects all-identical repeating digits (e.g. 0000000000, 1111111111).
    """
    if not value:
        return value
    raw = str(value).strip()
    if not raw:
        return raw

    # Explicitly disallow letters and words (e.g. "text", "mobile", "hello")
    if re.search(r"[a-zA-Z]", raw):
        raise ValidationError("Mobile number cannot contain letters or words. Please enter digits only.")

    # Only allow +, digits, spaces, hyphens, dots, and parentheses
    if not re.match(r"^[\+]?[\d\s\-\.\(\)]+$", raw):
        raise ValidationError(
            "Mobile number contains invalid characters. Use only digits, spaces, hyphens, and optional '+' prefix."
        )

    # Extract digits only
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        raise ValidationError(
            f"Mobile number is too short ({len(digits)} digits). Please enter a valid number with at least 7 digits."
        )
    if len(digits) > 15:
        raise ValidationError(
            f"Mobile number is too long ({len(digits)} digits). International standard allows at most 15 digits."
        )

    # Disallow all-same repeating digits (e.g. 0000000000, 1111111111)
    if len(set(digits)) == 1:
        raise ValidationError(
            "Please enter a valid, active mobile number (cannot consist of repeating identical digits)."
        )

    # Clean multiple spaces and return clean formatted representation
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return cleaned


def validate_full_name(value):
    """
    Validates candidate's full legal name:
    - Minimum 2 characters.
    - Must contain letters (not pure numbers or punctuation).
    - Disallows HTML/script tags or control characters.
    - Allows unicode letters, spaces, hyphens, apostrophes, and periods (e.g. J. Doe, O'Connor, Mary-Jane).
    """
    if not value:
        return value
    name = re.sub(r"\s+", " ", str(value)).strip()
    if len(name) < 2:
        raise ValidationError("Full legal name must be at least 2 characters long.")
    if len(name) > 150:
        raise ValidationError("Full legal name cannot exceed 150 characters.")

    # Must contain at least one letter, in any script (Tamil, Telugu, Arabic, CJK, ...).
    if not any(ch.isalpha() for ch in name):
        raise ValidationError("Full legal name must contain letters.")

    # Reject HTML or dangerous control characters
    if re.search(r"[<>{}\[\]\\;/=]", name):
        raise ValidationError("Full legal name cannot contain special symbols or HTML tags.")

    # Letters and combining marks of any script (Indic vowel signs are marks), spaces, dots, hyphens, apostrophes.
    for ch in name:
        # Digits stay allowed (as before) for names like "Candidate 2"; a letter is still required above.
        if ch in " .'-’" or unicodedata.category(ch)[0] in ("L", "M") or ch.isdigit():
            continue
        raise ValidationError(
            "Full legal name contains invalid characters. Use letters, spaces, hyphens, apostrophes, or periods only."
        )

    return name
