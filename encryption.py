from operator import methodcaller
import time
import json
import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from base64 import b64encode, b64decode
import textwrap
import random
from datetime import datetime
import string

PUBLIC_KEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArKZOdKQAL+iYzJ4Q5EQzwv/yvVPnfdNVKRgNG19HbCYM4qIzFPEOFv28SVFQh+xqAj8tAfjpMSTihFwt6BQuWfZXWYpAqf4jF4cU7ez/VHJyzsn8Cb7Lf/1KsLpuz+MbqufrA57AysnLAnRXHOwik+QnpsXZYjTcjgxQ0iLMe5iJyo06CKFxH1rmgYMwS4E89kNg1VtYrFKs1MajApfhu9hTEXnm/lP24TPdefRXbf+z84p1GLue2HRhZs3wECH1HJWZOsrdL/M+wigWldY0fHoiaKsjD9rK1NyaPtk4bIYuwPsfQu5RN4hkEPpTvdw1nKzOdo77zNa5ovCY0uNLZwIDAQAB"
PRIVATE_KEY = "MIICdQIBADANBgkqhkiG9w0BAQEFAASCAl8wggJbAgEAAoGBALqTtO2th/cDrjGe\n6rI9+vZcNT2Ad7LLyUXqCvs++jkK/fTlDbWhb+jLSd8zYnG7aJfvf00DZqVVnVhs\nIFOHyxj04dH0AFq8P/3D8hRyIKxi83fV5PcmdiyKjDtRhCIZdb12t9Q7ZQe0hZ9o\nGxOUrXvZxMObobfs9PaIvDtgLBv9AgMBAAECgYEAmY/Atm7BHXmWAZk+qf2BdQtn\ndnsqCarYugZri2bGI/T8GHf91KROq1fGMULln54T9BlWuVg3P5U7r+8unBGEAlqi\nTVYg8IkKlXEnea5aoQGiAgsJE00TPSjh5SN1E+pwAfVvOR60BvBP0SOrirWTFHvd\nsn164ck6RthkzwbqyrECQQDdOV26G0pkQq0dqs0O3Tf76T2rPIV9e/ALcDKWH17o\nOEshAX/ceOW+ibCGzkuPhzNQ/Ax/HQRzpwnCs5j9kr4XAkEA1+gM/SKPPiMtK2jF\nsvAn24fiXER7WchuNxam63NiF1icZYoQzCHZiPvLGPoYLzqMbSIGKjflIrfCuu4A\nn5E3CwI/UTq/L5bVufrBliMhKFQSOm1qN3YBjhH0Ej8gU/8w2hPRgk0Kpem+cjgc\nmWMmW41JSnNCI2qEN/MdomBPzMwVAkAeRA5GEekomb/EPQyxt86ujI7jxknQXTNu\nD4PYr7UJdsd9yGvwYbJf7IQc/ToUrM1wovtgQbOiOUZX8/MsayvfAkBy+iFMNTeV\nvSHNi2lVSpF9tTxgh7L0LC0kBkxKuldLVGS46Mh5new2sWcQX75ChroJ2UtZHlfq\nW02+EOY7mQkt"

# =====================
# 格式化 RSA key
# =====================
def format_rsa_key(key_b64: str, key_type: str) -> str:
    key_b64 = key_b64.replace("\n", "").replace(" ", "")
    if key_type == "PUBLIC":
        header, footer = "-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----"
    else:
        header, footer = "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"
    return f"{header}\n" + "\n".join(textwrap.wrap(key_b64, 64)) + f"\n{footer}"

# =====================
# RSA 长文本加解密
# =====================
def rsa_encrypt(data: str, pubkey_b64: str) -> str:
    pubkey = format_rsa_key(pubkey_b64, "PUBLIC")
    key = RSA.importKey(pubkey)
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(data.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")

def rsa_decrypt(cipher_b64: str, privkey_b64: str) -> str:
    privkey = format_rsa_key(privkey_b64, "PRIVATE")
    key = RSA.importKey(privkey)
    cipher = PKCS1_v1_5.new(key)
    cipher_bytes = b64decode(cipher_b64)
    return cipher.decrypt(cipher_bytes, None).decode("utf-8")

def rsa_encrypt_long(data: str, pubkey_b64: str) -> str:
    pubkey = format_rsa_key(pubkey_b64, "PUBLIC")
    key = RSA.importKey(pubkey)
    cipher = PKCS1_v1_5.new(key)
    data_bytes = data.encode("utf-8")

    max_chunk = key.size_in_bytes() - 11  # PKCS#1 填充后最大明文长度
    chunks = [data_bytes[i:i+max_chunk] for i in range(0, len(data_bytes), max_chunk)]
    enc_chunks = [cipher.encrypt(c) for c in chunks]
    return b64encode(b"".join(enc_chunks)).decode("utf-8")

def rsa_decrypt_long(cipher_b64: str, privkey_b64: str) -> str:
    privkey = format_rsa_key(privkey_b64, "PRIVATE")
    key = RSA.importKey(privkey)
    cipher = PKCS1_v1_5.new(key)

    cipher_bytes = b64decode(cipher_b64)
    chunk_size = key.size_in_bytes()
    chunks = [cipher_bytes[i:i+chunk_size] for i in range(0, len(cipher_bytes), chunk_size)]
    decrypted_chunks = [cipher.decrypt(c, None) for c in chunks]
    return b"".join(decrypted_chunks).decode("utf-8")

# =====================
# 生成随机 AES key
# =====================
def get_key(length=16):
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

# =====================
# AES 加密
# =====================

def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    """PKCS7 填充"""
    pad_len = block_size - len(data) % block_size
    return data + bytes([pad_len] * pad_len)

def pkcs7_unpad(data: bytes) -> bytes:
    """去除 PKCS7 填充"""
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("填充长度不合法")
    return data[:-pad_len]

def aes_encrypt(data, key: str) -> str:
    """
    AES ECB + PKCS7
    data: str 或 dict
    key: 16/24/32 字节的字符串
    返回 base64 字符串
    """
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False)
    data_bytes = data.encode("utf-8")
    key_bytes = key.encode("utf-8")

    cipher = AES.new(key_bytes, AES.MODE_ECB)
    padded = pkcs7_pad(data_bytes)
    encrypted = cipher.encrypt(padded)
    return b64encode(encrypted).decode("utf-8")
    

# =====================
# 测试加解密
# =====================
if __name__ == "__main__":
    body = {"venTypeId":"7d46c0a4-3ae6-4398-822b-d4b7b37085fa","venueId":"d784ad7c-cb24-4282-afd6-a67aec68c675","fieldType":"健身房","returnUrl":"https://sports.sjtu.edu.cn/#/paymentResult/1","scheduleDate":"2026-04-30","week":"4","spaces":[{"count":1,"venuePrice":"0","status":1,"scheduleTime":"20:00-21:00","subSitename":"场地1","subSiteId":"example-field-id","tensity":"1","venueNum":1}],"tenSity":"紧张"}
    aes_key = 'CER22QR2WABYMJ8C'
    a = int(datetime.now().timestamp() * 1000)
    print(a)
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Content-Length": "728",
        "sid": rsa_encrypt(aes_key, PUBLIC_KEY),
        "tim": rsa_encrypt(str(a), PUBLIC_KEY),
        "cookie": "JSESSIONID=<你自己的会话ID>",
        'User-Agent': "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.4638.69 Safari/537.36",
        'sec-ch-ua-platform': "macOS",
        'Origin': "https://sports.sjtu.edu.cn",
        'Sec-Fetch-Site': "same-origin",
        'Sec-Fetch-Mode': "cors",
        'Sec-Fetch-Dest': "empty",
        'Referer': "https://sports.sjtu.edu.cn/pc/",
    }
    encrypted_body = aes_encrypt(body, aes_key)
    url = "https://sports.sjtu.edu.cn/venue/personal/ConfirmOrder"
    response = requests.post(url, headers=headers, data=encrypted_body)
    print(response.status_code)
    print("响应内容:", response.text)
