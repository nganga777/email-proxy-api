import os
import uuid
import smtplib
import socks
import socket
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

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

async def get_proxy_ip(proxy_host: str, proxy_port: int) -> dict:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((proxy_host, proxy_port))
            proxy_ip = s.getpeername()[0]
            return {
                "success": True,
                "proxyIP": proxy_ip
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

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

    # Set up proxy if provided
    if req.proxyConfig and req.proxyConfig.host:
        try:
            proxy_ip_info = await get_proxy_ip(req.proxyConfig.host, req.proxyConfig.port)
            if proxy_ip_info["success"]:
                log_entry["afterProxyIp"] = proxy_ip_info["proxyIP"]
                use_proxy = True
                
                socks.setdefaultproxy()
                socks.setdefaultproxy(
                    socks.SOCKS5,
                    req.proxyConfig.host,
                    req.proxyConfig.port,
                    True,
                    req.proxyConfig.username,
                    req.proxyConfig.password
                )
                socket.socket = socks.socksocket
                log_entry["proxyUsed"] = True
        except Exception as e:
            log_entry["proxyError"] = str(e)
            log_entry["fallbackToDirect"] = True
            socks.setdefaultproxy()
            socket.socket = socket._socketobject
    else:
        log_entry["noProxyConfigured"] = True

    # Generate Message-ID
    message_id = f"<{uuid.uuid4()}@{req.smtpConfig.host}>"
    from_addr = f'"{req.senderName}" <{req.senderEmail}>'
    message = f"""\
From: {from_addr}
To: {req.toEmail}
Subject: {req.subject}
Message-ID: {message_id}
Content-Type: text/html

<p>Your verification code is: <strong>{req.code}</strong></p>
"""

    smtp_logs = []
    server = None
    try:
        # Create SMTP connection with enhanced logging
        server = smtplib.SMTP(timeout=30)
        server.connect(req.smtpConfig.host, req.smtpConfig.port)
        
        # Enhanced debug logging
        debug_msgs = []
        server.set_debuglevel(1)
        
        # Custom debug handler that captures timestamps
        def debug_handler(*args):
            timestamp = datetime.utcnow().isoformat()
            debug_msg = " ".join(str(arg) for arg in args)
            debug_msgs.append(f"[{timestamp}] {debug_msg}")
        
        server._debug_smtp = debug_handler
        
        # SMTP handshake
        code, msg = server.ehlo()
        if code != 250:
            server.helo()
        
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            server.ehlo()
        
        # Connection verification
        try:
            server.noop()
            log_entry["connectionVerified"] = True
        except Exception as verify_error:
            log_entry["connectionVerified"] = False
            log_entry["verifyError"] = str(verify_error)
        
        # Authentication and sending
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        server.sendmail(from_addr, [req.toEmail], message)
        
        # Store enhanced logs
        log_entry.update({
            "smtpLogs": debug_msgs,
            "connectionType": "proxy" if use_proxy else "direct",
            "finalOutcome": "success",
            "smtpSuccess": True
        })
        
        return {
            "success": True,
            "messageId": message_id,
            "logs": log_entry
        }
        
    except smtplib.SMTPException as e:
        log_entry.update({
            "smtpLogs": smtp_logs,
            "smtpError": str(e),
            "finalOutcome": "error",
            "smtpSuccess": False
        })
        return {
            "success": False,
            "error": str(e),
            "logs": log_entry
        }
    except Exception as e:
        log_entry.update({
            "smtpLogs": smtp_logs,
            "unexpectedError": str(e),
            "finalOutcome": "error",
            "smtpSuccess": False
        })
        return {
            "success": False,
            "error": str(e),
            "logs": log_entry
        }
    finally:
        try:
            if server:
                server.quit()
        except:
            pass
        # Reset proxy settings
        socks.setdefaultproxy()
        socket.socket = socket._socketobject

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
