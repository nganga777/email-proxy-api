import os
import uuid
import smtplib
import socket
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from logging.config import dictConfig
import socks  # PySocks library

# Logging configuration (same as before)
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

# Model definitions (same as before)
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

def create_proxy_socket(proxy_config: ProxyConfig, timeout=20):
    """Create a socket that connects through the proxy"""
    sock = socks.socksocket()
    sock.set_proxy(
        proxy_type=socks.SOCKS5,
        addr=proxy_config.host,
        port=proxy_config.port,
        username=proxy_config.username,
        password=proxy_config.password
    )
    sock.settimeout(timeout)
    return sock

def create_smtp_connection(smtp_config: SMTPConfig, proxy_config: Optional[ProxyConfig] = None):
    """Create SMTP connection with optional proxy"""
    if proxy_config:
        # Create proxy socket first
        sock = create_proxy_socket(proxy_config)
        sock.connect((smtp_config.host, smtp_config.port))
        
        # Create SMTP connection with existing socket
        server = smtplib.SMTP()
        server.sock = sock
        server.connect(smtp_config.host, smtp_config.port)
    else:
        # Regular direct connection
        server = smtplib.SMTP(
            host=smtp_config.host,
            port=smtp_config.port,
            timeout=20
        )
    
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

    try:
        server = create_smtp_connection(req.smtpConfig, req.proxyConfig)
        server.ehlo()
        
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            server.ehlo()
        
        # Verify connection
        try:
            server.noop()
            log_entry["connectionVerified"] = True
        except Exception as verify_error:
            log_entry["connectionVerified"] = False
            log_entry["verifyError"] = str(verify_error)
            raise verify_error
        
        # Authenticate and send email
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        
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
