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
    proxy_agent = None

    # Set up proxy if provided
    if req.proxyConfig and req.proxyConfig.host:
        try:
            proxy_ip_info = await get_proxy_ip(req.proxyConfig.host, req.proxyConfig.port)
            if proxy_ip_info["success"]:
                log_entry["afterProxyIp"] = proxy_ip_info["proxyIP"]
                use_proxy = True
                
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
            log_entry["proxyError"] = str(e)
            log_entry["fallbackToDirect"] = True
    else:
        log_entry["noProxyConfigured"] = True

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

    smtp_logs = []
    server = None
    try:
        # Create SMTP connection with simplified logging
        smtp_logs.append(f"Connecting to SMTP server at {req.smtpConfig.host}:{req.smtpConfig.port}")
        server = smtplib.SMTP(req.smtpConfig.host, req.smtpConfig.port, timeout=20)
        smtp_logs.append("SMTP connection established")
        
        # Minimal debug logging for critical events only
        debug_msgs = []
        server.set_debuglevel(0)  # Disable verbose logging
        
        def log_critical_events(*args):
            msg = ' '.join(str(x) for x in args)
            if "235" in msg:  # Authentication success
                debug_msgs.append("SMTP authentication successful")
            elif "250" in msg and "MAIL FROM" in msg:  # Sender accepted
                debug_msgs.append("Sender address accepted")
            elif "250" in msg and "RCPT TO" in msg:  # Recipient accepted
                debug_msgs.append("Recipient address accepted")
            elif "250" in msg and "Mail accepted" in msg:  # Message accepted
                debug_msgs.append("Message content accepted")
            elif "221" in msg:  # Connection closing
                debug_msgs.append("SMTP connection closing normally")
        
        server._debug_smtp = log_critical_events
        
        # SMTP handshake
        server.ehlo()
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            server.starttls()
            server.ehlo()
            smtp_logs.append("TLS encryption established")

        # Verify connection
        try:
            server.noop()
            smtp_logs.append("Server connection verified")
            log_entry["connectionVerified"] = True
        except Exception as verify_error:
            smtp_logs.append(f"Connection verification failed: {str(verify_error)}")
            log_entry["connectionVerified"] = False
            log_entry["verifyError"] = str(verify_error)
        
        # Authentication
        smtp_logs.append(f"Authenticating as {req.smtpConfig.auth.user}")
        server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        smtp_logs.append("Authentication successful")
        
        # Send email
        server.sendmail(from_addr, [to_addr], message)
        smtp_logs.append("Email successfully submitted to server")
        
        # Close connection
        server.quit()
        smtp_logs.append("Connection closed normally")
        
        # Combine all logs
        smtp_logs.extend(debug_msgs)
        log_entry["smtpLogs"] = smtp_logs
        log_entry["connectionType"] = "proxy" if use_proxy else "direct"
        log_entry["finalOutcome"] = "success"
        log_entry["smtpSuccess"] = True
        
        return {
            "success": True,
            "messageId": message_id,
            "logs": log_entry
        }
        
    except smtplib.SMTPException as e:
        smtp_logs.append(f"SMTP protocol error: {str(e)}")
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
        smtp_logs.append(f"Unexpected error: {str(e)}")
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
