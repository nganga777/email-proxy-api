import os
import uuid
import smtplib
import socks
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional
import datetime

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

def get_proxy_ip(proxy_config: ProxyConfig) -> Optional[str]:
    try:
        s = socks.socksocket()
        s.set_proxy(
            socks.SOCKS5,
            proxy_config.host,
            proxy_config.port,
            username=proxy_config.username,
            password=proxy_config.password
        )
        s.settimeout(5)
        s.connect(("ifconfig.me", 80))
        s.sendall(b"GET /ip HTTP/1.1\r\nHost: ifconfig.me\r\n\r\n")
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        ip = data.split(b"\r\n\r\n")[-1].decode().strip()
        return ip
    except Exception:
        return None

@app.post("/send-email")
def send_email(req: EmailRequest, request: Request):
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "originalIp": req.originalIp or request.client.host,
        "beforeProxyIp": request.client.host,
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

    smtp_logs = []
    use_proxy = False

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
            use_proxy = True
        except Exception as e:
            log_entry["proxyError"] = repr(e)
            log_entry["afterProxyIp"] = None
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
        smtp_logs.append("SMTP connection established")
        server.ehlo()
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            smtp_logs.append("STARTTLS completed")
            server.ehlo()
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        smtp_logs.append("SMTP login successful")
        server.sendmail(from_addr, [to_addr], message)
        smtp_logs.append("Email sent")
        server.quit()
        smtp_logs.append("SMTP connection closed")
        log_entry["smtpSuccess"] = True
        log_entry["smtpLogs"] = smtp_logs
        log_entry["connectionType"] = "proxy" if use_proxy else "direct"
        log_entry["finalOutcome"] = "success"
    except Exception as e:
        smtp_logs.append(f"Error sending email: {e}")
        log_entry["smtpSuccess"] = False
        log_entry["smtpError"] = repr(e)
        log_entry["smtpLogs"] = smtp_logs
        log_entry["afterProxyIp"] = None
        return {"success": False, "error": str(e), "logs": log_entry}

    # Now, after sending the email, check the proxy IP if proxy was used
    if use_proxy:
        after_proxy_ip = get_proxy_ip(req.proxyConfig)
        log_entry["afterProxyIp"] = after_proxy_ip if after_proxy_ip else None
    else:
        log_entry["afterProxyIp"] = None

    return {
        "success": True,
        "messageId": message_id,
        "logs": log_entry
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
