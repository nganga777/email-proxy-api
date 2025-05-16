import os
import uuid
import smtplib
import socks
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

class SMTPAuth(BaseModel):
    user: str
    password: str

class SMTPConfig(BaseModel):
    host: str
    port: int
    secure: bool = False
    auth: SMTPAuth

class ProxyConfig(BaseModel):
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

class EmailRequest(BaseModel):
    smtpConfig: SMTPConfig
    proxyConfig: Optional[ProxyConfig] = None
    senderName: str
    senderEmail: str
    toEmail: str
    subject: str
    code: str
    originalIp: Optional[str] = None

@app.post("/send-email")
def send_email(req: EmailRequest, request: Request):
    log_entry = {
        "originalIp": req.originalIp or request.client.host,
        "proxyConfig": {
            "host": getattr(req.proxyConfig, "host", None),
            "port": getattr(req.proxyConfig, "port", None),
            "hasAuth": bool(getattr(req.proxyConfig, "username", None) and getattr(req.proxyConfig, "password", None))
        },
        "requestData": {
            "toEmail": req.toEmail,
            "senderEmail": req.senderEmail,
            "subject": req.subject
        }
    }

    # Set up proxy if provided (do this FIRST, before any other socket/smtplib operation)
    if req.proxyConfig and req.proxyConfig.host:
        try:
            socks.setdefaultproxy(
                socks.SOCKS5,
                req.proxyConfig.host,
                req.proxyConfig.port,
                True,
                req.proxyConfig.username,
                req.proxyConfig.password
            )
            socks.wrapmodule(smtplib)
            log_entry["proxyUsed"] = True
        except Exception as e:
            log_entry["proxyError"] = repr(e)
            return {"success": False, "error": f"Proxy setup failed: {e}", "logs": log_entry}
    else:
        log_entry["proxyUsed"] = False

    # Generate Message-ID
    message_id = f"<{uuid.uuid4()}@{req.smtpConfig.host}>"
    from_addr = f'"{req.senderName}" <{req.senderEmail}>'
    to_addr = req.toEmail
    subject = req.subject
    code = req.code
    message = f"""\
From: {from_addr}
To: {to_addr}
Subject: {subject}
Message-ID: {message_id}
Content-Type: text/html

<p>Your verification code is: <strong>{code}</strong></p>
"""

    try:
        server = smtplib.SMTP(req.smtpConfig.host, req.smtpConfig.port, timeout=20)
        server.ehlo()
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            server.ehlo()
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        server.sendmail(from_addr, [to_addr], message)
        server.quit()
        log_entry["smtpSuccess"] = True
        return {
            "success": True,
            "messageId": message_id,
            "logs": log_entry
        }
    except Exception as e:
        log_entry["smtpSuccess"] = False
        log_entry["smtpError"] = repr(e)
        return {"success": False, "error": str(e), "logs": log_entry}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
