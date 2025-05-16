import os
import uuid
import smtplib
import socket
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import socks  # PySocks library

app = FastAPI()

# Model definitions
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

def create_proxy_socket(proxy_config: ProxyConfig, smtp_host: str, smtp_port: int):
    """Create and connect a socket through the proxy"""
    sock = socks.socksocket()
    sock.set_proxy(
        proxy_type=socks.SOCKS5,
        addr=proxy_config.host,
        port=proxy_config.port,
        username=proxy_config.username,
        password=proxy_config.password
    )
    sock.settimeout(20)
    sock.connect((smtp_host, smtp_port))
    return sock

def create_smtp_connection(smtp_config: SMTPConfig, proxy_config: Optional[ProxyConfig] = None):
    """Create SMTP connection with optional proxy"""
    if proxy_config:
        # Create and connect proxy socket
        sock = create_proxy_socket(proxy_config, smtp_config.host, smtp_config.port)
        
        # Create SMTP connection with existing socket
        server = smtplib.SMTP(timeout=20)
        server.sock = sock
        server.connect(smtp_config.host, smtp_config.port)
    else:
        # Regular direct connection
        server = smtplib.SMTP(smtp_config.host, smtp_config.port, timeout=20)
    
    server.set_debuglevel(1)
    return server

@app.post("/send-email")
async def send_email(req: EmailRequest, request: Request):
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "originalIp": req.originalIp or request.client.host,
        "proxyConfig": {
            "host": getattr(req.proxyConfig, "host", None),
            "port": getattr(req.proxyConfig, "port", None),
            "hasAuth": bool(getattr(req.proxyConfig, "username", None) and getattr(req.proxyConfig, "password", None))
        }
    }

    try:
        server = create_smtp_connection(req.smtpConfig, req.proxyConfig)
        server.ehlo()
        
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            server.ehlo()
        
        # Get the actual connection IP (should be proxy IP if using proxy)
        connection_ip = server.sock.getpeername()[0]
        log_entry["connectionIp"] = connection_ip

        # Authenticate
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        
        # Create email with headers that will show the proxy IP
        message_id = f"<{uuid.uuid4()}@{req.smtpConfig.host}>"
        from_addr = f'"{req.senderName}" <{req.senderEmail}>'
        to_addr = req.toEmail
        subject = req.subject
        code = req.code
        
        message = f"""\
Received: from {connection_ip} (via proxy)
Message-ID: {message_id}
From: {from_addr}
To: {to_addr}
Subject: {subject}
Date: {datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S +0000')}
Content-Type: text/html

<p>Your verification code is: <strong>{code}</strong></p>
"""
        
        server.sendmail(from_addr, [to_addr], message)
        server.quit()
        
        return {
            "success": True,
            "messageId": message_id,
            "connectionIp": connection_ip,
            "logs": log_entry
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "logs": log_entry
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
