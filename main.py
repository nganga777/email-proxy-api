import os
import uuid
import smtplib
import socket
import socks  # PySocks
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

app = FastAPI()

# Model definitions remain the same
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

def get_smtp_connection(smtp_config: SMTPConfig, proxy_config: Optional[ProxyConfig] = None):
    """Create SMTP connection with optional proxy"""
    if proxy_config:
        # Create a new socket that will use the proxy
        sock = socks.socksocket()
        sock.set_proxy(
            proxy_type=socks.SOCKS5,
            addr=proxy_config.host,
            port=proxy_config.port,
            username=proxy_config.username,
            password=proxy_config.password
        )
        
        # Connect through proxy
        sock.connect((smtp_config.host, smtp_config.port))
        
        # Create SMTP connection with existing socket
        smtp = smtplib.SMTP()
        smtp.sock = sock
        smtp.connect(smtp_config.host, smtp_config.port)
    else:
        # Regular direct connection
        smtp = smtplib.SMTP(smtp_config.host, smtp_config.port)
    
    return smtp

@app.post("/send-email")
async def send_email(req: EmailRequest, request: Request):
    try:
        # Create the connection
        server = get_smtp_connection(req.smtpConfig, req.proxyConfig)
        
        # Verify proxy is being used by checking the peer address
        if req.proxyConfig:
            peer_addr = server.sock.getpeername()[0]
            print(f"Connected via proxy. Peer address: {peer_addr}")
        
        # Standard SMTP flow
        server.ehlo()
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            server.ehlo()
        
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        
        # Create standard email without header modifications
        message_id = f"<{uuid.uuid4()}@{req.smtpConfig.host}>"
        message = f"""\
From: "{req.senderName}" <{req.senderEmail}>
To: {req.toEmail}
Subject: {req.subject}
Message-ID: {message_id}
Content-Type: text/html

<p>Your verification code is: <strong>{req.code}</strong></p>
"""
        server.sendmail(req.senderEmail, [req.toEmail], message)
        server.quit()
        
        return {"success": True, "messageId": message_id}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
