from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


def _validate_bcrypt_password(value: str) -> str:
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Password must not exceed 72 UTF-8 bytes")
    return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=512, repr=False)

    _password_fits_bcrypt = field_validator("password")(_validate_bcrypt_password)


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str


class LoginResponse(BaseModel):
    access_token: str | None = Field(default=None, repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    expires_in: int | None = None
    token_type: str = "bearer"
    challenge_token: str | None = Field(default=None, repr=False)
    mfa_required: bool = False
    mfa_setup_required: bool = False
    user_id: UUID
    role: str
    locale: str
    organization: OrganizationOut


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    role: str
    locale: str
    is_active: bool
    mfa_enabled: bool
    organization_id: UUID
    created_at: datetime | None = None

class MeOut(UserOut):
    organization: OrganizationOut


class AssignableUserOut(BaseModel):
    id: UUID
    email: EmailStr
    role: str


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=512, repr=False)
    role: str = "recruiter"
    locale: str = "ru"

    _password_fits_bcrypt = field_validator("password")(_validate_bcrypt_password)


class UserInvitationCreate(BaseModel):
    email: EmailStr
    role: str = "recruiter"
    locale: str = "ru"
    expires_in_days: int = Field(default=7, ge=1, le=30)


class UserAccessCreatedOut(BaseModel):
    id: UUID
    user_id: UUID
    purpose: str
    token_prefix: str
    expires_at: datetime
    access_url: str = Field(repr=False)
    delivery_enqueued: bool


class UserAccessPreviewOut(BaseModel):
    purpose: str
    email: EmailStr
    role: str
    locale: str
    organization_name: str
    expires_at: datetime


class InvitationAcceptRequest(BaseModel):
    password: str = Field(min_length=8, max_length=512, repr=False)

    _password_fits_bcrypt = field_validator("password")(_validate_bcrypt_password)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    password: str = Field(min_length=8, max_length=512, repr=False)

    _password_fits_bcrypt = field_validator("password")(_validate_bcrypt_password)


class PasswordResetAcceptedOut(BaseModel):
    accepted: bool = True


class UserUpdate(BaseModel):
    role: str | None = None
    locale: str | None = None
    is_active: bool | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512, repr=False)
    new_password: str = Field(min_length=8, max_length=512, repr=False)

    _passwords_fit_bcrypt = field_validator(
        "current_password", "new_password"
    )(_validate_bcrypt_password)


class MfaSetupRequest(BaseModel):
    challenge_token: str = Field(min_length=1, max_length=4096, repr=False)


class MfaSetupResponse(BaseModel):
    # Returned once, before confirmation. It is never included in user/session
    # DTOs and is not recoverable through the API after confirmation.
    secret: str = Field(repr=False)
    provisioning_uri: str = Field(repr=False)


class MfaConfirmRequest(BaseModel):
    challenge_token: str = Field(min_length=1, max_length=4096, repr=False)
    code: str = Field(pattern=r"^\d{6}$", repr=False)


class MfaConfirmResponse(LoginResponse):
    recovery_codes: list[str] = Field(repr=False)


class MfaVerifyRequest(BaseModel):
    challenge_token: str = Field(min_length=1, max_length=4096, repr=False)
    code: str | None = Field(default=None, pattern=r"^\d{6}$", repr=False)
    recovery_code: str | None = Field(
        default=None, min_length=1, max_length=64, repr=False
    )

    @model_validator(mode="after")
    def exactly_one_verifier(self) -> "MfaVerifyRequest":
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("Provide exactly one MFA verifier")
        return self


class MfaDisableRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512, repr=False)
    code: str | None = Field(default=None, pattern=r"^\d{6}$", repr=False)
    recovery_code: str | None = Field(
        default=None, min_length=1, max_length=64, repr=False
    )

    _password_fits_bcrypt = field_validator("current_password")(
        _validate_bcrypt_password
    )

    @model_validator(mode="after")
    def exactly_one_verifier(self) -> "MfaDisableRequest":
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("Provide exactly one MFA verifier")
        return self


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512, repr=False)


class RefreshSessionOut(BaseModel):
    id: UUID
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime
    current: bool
