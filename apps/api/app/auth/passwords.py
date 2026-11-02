import bcrypt

# bcrypt has a 72-byte limit on the password input.
_MAX_BYTES = 72


def password_bytes(plain: str) -> bytes:
    encoded = plain.encode("utf-8")
    if len(encoded) > _MAX_BYTES:
        raise ValueError("Password must not exceed 72 UTF-8 bytes")
    return encoded


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(password_bytes(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password_bytes(plain), hashed.encode("utf-8"))
    except ValueError:
        return False
