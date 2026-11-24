"""Dedicated 163 SMTP transport. Windows DPAPI protects the saved app password."""

import ctypes
from ctypes import wintypes
import json
import smtplib
import ssl


class DataBlob(ctypes.Structure):
    _fields_ = [('length',wintypes.DWORD),('data',ctypes.c_void_p)]


class BeforeSubmission(OSError):
    """The transport failed before the message could be submitted."""


def unprotect_password(secret):
    encrypted = bytes.fromhex(secret)
    buffer = ctypes.create_string_buffer(encrypted)
    source = DataBlob(len(encrypted),ctypes.cast(buffer,ctypes.c_void_p))
    plaintext = DataBlob()
    crypt32 = ctypes.WinDLL('crypt32',use_last_error=True)
    if not crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(plaintext)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(plaintext.data,plaintext.length).decode('utf-16-le')
    finally:
        ctypes.memset(plaintext.data,0,plaintext.length)
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(plaintext.data)


def send(config_path, message):
    attempted = accepted = False
    try:
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        message['From'] = config['sender']
        message['To'] = config['recipient']
        secret = config.get('protected_password')
        with smtplib.SMTP_SSL('smtp.163.com',465,timeout=15,context=ssl.create_default_context()) as smtp:
            if secret:
                smtp.login(config['sender'],unprotect_password(secret))
            attempted = True
            if smtp.send_message(message):
                raise smtplib.SMTPException('收件地址被拒绝')
            accepted = True
    except (OSError,ValueError,KeyError,smtplib.SMTPException) as exc:
        if accepted:
            return
        if not attempted or isinstance(exc,(smtplib.SMTPSenderRefused,smtplib.SMTPRecipientsRefused)):
            raise BeforeSubmission(type(exc).__name__) from exc
        raise
