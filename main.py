import os
import uuid
import smtplib
import socks
import socket
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import asyncio

app = FastAPI()

# Configuration - set DEBUG_SMTP=false in production for best performance
DEBUG_SMTP = os.getenv("DEBUG_SMTP", "false").lower() == "true"
PROXY_IP_TIMEOUT = 5
SMTP_CONNECT_TIMEOUT = 20
SMTP_LOGIN_TIMEOUT = 10
SMTP_SEND_TIMEOUT = 30
TOTAL_REQUEST_TIMEOUT = 60

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

class SMTPDebugLogger:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.logs: List[str] = []
    
    def __call__(self, *args):
        if self.enabled:
            self.logs.append(" ".join(str(arg) for arg in args))

async def get_proxy_ip(proxy_host: str, proxy_port: int) -> dict:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(PROXY_IP_TIMEOUT)
            s.connect((proxy_host, proxy_port))
            proxy_ip = s.getpeername()[0]
            return {"success": True, "proxyIP": proxy_ip}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/send-email")
async def send_email(req: EmailRequest, request: Request):
    async def _send_email():
        debug_logger = SMTPDebugLogger(DEBUG_SMTP)
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "originalIp": req.originalIp or request.client.host,
            "beforeProxyIp": request.client.host,
            "proxyConfig": {
                "host": getattr(req.proxyConfig, "host", None),
                "port": getattr(req.proxyConfig, "port", None),
                "hasAuth": bool(getattr(req.proxyConfig, "username", None) and 
                              getattr(req.proxyConfig, "password", None))
            },
            "requestData": {
                "toEmail": req.toEmail,
                "senderEmail": req.senderEmail,
                "subject": req.subject
            }
        }

        use_proxy = False

        # Proxy setup
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
                else:
                    raise Exception('Failed to get proxy IP')
            except Exception as e:
                log_entry["proxyError"] = str(e)
                log_entry["fallbackToDirect"] = True
        else:
            log_entry["noProxyConfigured"] = True

        # Email message setup
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

        server = None
        try:
            # SMTP connection
            server = smtplib.SMTP(req.smtpConfig.host, req.smtpConfig.port, timeout=SMTP_CONNECT_TIMEOUT)
            
            if DEBUG_SMTP:
                server.set_debuglevel(1)
                server._debug_smtp = debug_logger
            
            server.ehlo()
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
            
            # Successful response
            return {
                "success": True,
                "messageId": message_id,
                "logs": {
                    **log_entry,
                    "smtpLogs": debug_logger.logs if DEBUG_SMTP else ["SMTP debugging disabled"],
                    "connectionType": "proxy" if use_proxy else "direct",
                    "finalOutcome": "success",
                    "smtpSuccess": True
                }
            }

        except smtplib.SMTPException as e:
            # SMTP-specific errors
            return {
                "success": False,
                "error": str(e),
                "logs": {
                    **log_entry,
                    "smtpLogs": debug_logger.logs if DEBUG_SMTP else ["SMTP debugging disabled"],
                    "smtpError": str(e),
                    "finalOutcome": "error",
                    "smtpSuccess": False
                }
            }
        except socket.timeout as e:
            # Timeout errors
            return {
                "success": False,
                "error": f"Timeout occurred: {str(e)}",
                "logs": {
                    **log_entry,
                    "smtpLogs": debug_logger.logs if DEBUG_SMTP else ["SMTP debugging disabled"],
                    "timeoutError": str(e),
                    "finalOutcome": "timeout",
                    "smtpSuccess": False
                }
            }
        except Exception as e:
            # Unexpected errors
            return {
                "success": False,
                "error": str(e),
                "logs": {
                    **log_entry,
                    "smtpLogs": debug_logger.logs if DEBUG_SMTP else ["SMTP debugging disabled"],
                    "unexpectedError": str(e),
                    "finalOutcome": "error",
                    "smtpSuccess": False
                }
            }
        finally:
            if server:
                try:
                    server.quit()
                except:
                    pass

    # Execute with overall timeout
    try:
        return await asyncio.wait_for(_send_email(), timeout=TOTAL_REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Overall request timed out after {TOTAL_REQUEST_TIMEOUT} seconds"
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 3000)),
        # Recommended production settings:
        workers=int(os.environ.get("WEB_CONCURRENCY", 1)),
        limit_max_requests=1000,
        timeout_keep_alive=2
    )
