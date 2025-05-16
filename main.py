import os
import uuid
import smtplib
import socks
import socket
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from logging.config import dictConfig

# Logging configuration
logging_config = {
    "version": 1,
    "formatters": {
        "default": {
            "format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "default"
        }
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"]
    }
}

dictConfig(logging_config)

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

async def get_proxy_ip(smtp_host: str, smtp_port: int, proxy_host: str, proxy_port: int, proxy_username: Optional[str] = None, proxy_password: Optional[str] = None) -> dict:
    try:
        socks.setdefaultproxy(
            socks.SOCKS5,
            proxy_host,
            proxy_port,
            True,
            proxy_username,
            proxy_password
        )
        socks.wrapmodule(smtplib)
        
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
        server.ehlo()
        
        proxy_ip = server.socket.getpeername()[0]
        server.quit()
        return {
            "success": True,
            "proxyIP": proxy_ip
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

def create_smtp_connection(smtp_config: SMTPConfig, proxy_config: Optional[ProxyConfig] = None):
    if proxy_config:
        socks.setdefaultproxy(
            socks.SOCKS5,
            proxy_config.host,
            proxy_config.port,
            True,
            proxy_config.username,
            proxy_config.password
        )
        socks.wrapmodule(smtplib)
    
    server = smtplib.SMTP(smtp_config.host, smtp_config.port, timeout=20)
    server.set_debuglevel(1)
    return server

@app.post("/send-email")
async def send_email(req: EmailRequest, request: Request):
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
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

    use_proxy = False
    if req.proxyConfig and req.proxyConfig.host:
        try:
            proxy_ip_info = await get_proxy_ip(req.smtpConfig.host, req.smtpConfig.port, req.proxyConfig.host, req.proxyConfig.port, req.proxyConfig.username, req.proxyConfig.password)
            if proxy_ip_info["success"]:
                log_entry["afterProxyIp"] = proxy_ip_info["proxyIP"]
                use_proxy = True
                log_entry["proxyUsed"] = True
                log_entry["connectionType"] = "proxy"
            else:
                log_entry["proxyError"] = proxy_ip_info["error"]
                log_entry["fallbackToDirect"] = True
                log_entry["connectionType"] = "direct"
        except Exception as e:
            log_entry["proxyError"] = str(e)
            log_entry["fallbackToDirect"] = True
            log_entry["connectionType"] = "direct"
    else:
        log_entry["noProxyConfigured"] = True
        log_entry["connectionType"] = "direct"

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
        server = create_smtp_connection(req.smtpConfig, req.proxyConfig)
        server.ehlo()
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            server.ehlo()
        
        try:
            server.noop()
            log_entry["connectionVerified"] = True
        except Exception as verify_error:
            log_entry["connectionVerified"] = False
            log_entry["verifyError"] = str(verify_error)
        
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        server.sendmail(from_addr, [to_addr], message)
        server.quit()
        
        log_entry["finalOutcome"] = "success"
        log_entry["smtpSuccess"] = True
        
        return {
            "success": True,
            "messageId": message_id,
            "logs": log_entry
        }
    except Exception as e:
        log_entry["smtpSuccess"] = False
        log_entry["smtpError"] = str(e)
        log_entry["finalOutcome"] = "error"
        return {
            "success": False,
            "error": str(e),
            "logs": log_entry
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
