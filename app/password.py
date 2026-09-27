import hashlib, secrets, base64, hmac

N=2**14
R=8
P=1

def hash_password(password: str) -> str:
    salt=secrets.token_bytes(16)
    digest=hashlib.scrypt(password.encode(),salt=salt,n=N,r=R,p=P)
    return "scrypt$%s$%s$%s$%s" % (N,R,P,base64.urlsafe_b64encode(salt).decode(),base64.urlsafe_b64encode(digest).decode())

def verify_password(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        salt=base64.urlsafe_b64decode(salt_b64.encode())
        digest=base64.urlsafe_b64decode(digest_b64.encode())
        check=hashlib.scrypt(password.encode(),salt=salt,n=int(n),r=int(r),p=int(p))
        return hmac.compare_digest(check,digest)
    except Exception:
        return False
