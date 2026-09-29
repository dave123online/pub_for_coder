from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models


def _get_fernet():
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        raise ValueError("FIELD_ENCRYPTION_KEY doit être défini (variable d'env) pour chiffrer les clés API.")
    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedCharField(models.CharField):
    """CharField chiffré au repos (Fernet). Déchiffré automatiquement à la lecture."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return _get_fernet().encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        return _get_fernet().decrypt(value.encode()).decode()

    def to_python(self, value):
        return value
