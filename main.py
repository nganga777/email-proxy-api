import os
import uuid
import smtplib
import socks
import socket
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import asyncio

app = FastAPI()

# Timeout constants (in seconds)
PROXY_IP_TIMEOUT = 5
SMTP_CONNECT_TIMEOUT = 20
SMTP_LOGIN_TIMEOUT = 10
SMTP_SEND_TIMEOUT = 30
TOTAL_REQUEST_TIMEOUT = 60  # Overall timeout for the entire request

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
            s.settimeout(PROXY_IP_TIMEOUT)
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

async def execute_with_timeout(coroutine, timeout, timeout_message):
    try:
        return await asyncio.wait_for(coroutine, timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=timeout_message)

@app.post("/send-email")
async def send_email(req: EmailRequest, request: Request):
    async def _send_email():
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
                # Get proxy IP information with timeout
                proxy_ip_info = await get_proxy_ip(req.proxyConfig.host, req.proxyConfig.port)
                if proxy_ip_info["success"]:
                    log_entry["afterProxyIp"] = proxy_ip_info["proxyIP"]
                    use_proxy = True
                    
                    # Configure SOCKS proxy
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
            # Create SMTP connection with timeout
            server = smtplib.SMTP(req.smtpConfig.host, req.smtpConfig.port, timeout=SMTP_CONNECT_TIMEOUT)
            
            # Log SMTP communication
            server.set_debuglevel(1)
            debug_msgs = []
            server._debug_smtp = lambda *args: debug_msgs.append(" ".join(str(x) for x in args))
            
            # EHLO/HELO with timeout
            server.ehlo()
            if req.smtpConfig.secure and req.smtpConfig.port == 587:
                server.starttls()
                server.ehlo()
            
            # Verify connection with timeout
            try:
                server.noop()
                log_entry["connectionVerified"] = True
            except Exception as verify_error:
                log_entry["connectionVerified"] = False
                log_entry["verifyError"] = str(verify_error)
            
            # Login with timeout
            server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
            
            # Send email with timeout
            server.sendmail(from_addr, [to_addr], message)
            
            # Collect SMTP logs
            smtp_logs = debug_msgs
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
            log_entry["smtpSuccess"] = False
            log_entry["smtpError"] = str(e)
            log_entry["finalOutcome"] = "error"
            if 'smtpLogs' not in log_entry:
                log_entry["smtpLogs"] = smtp_logs
            return {
                "success": False,
                "error": str(e),
                "logs": log_entry
            }
        except socket.timeout as e:
            log_entry["timeoutError"] = str(e)
            log_entry["finalOutcome"] = "timeout"
            return {
                "success": False,
                "error": f"Timeout occurred: {str(e)}",
                "logs": log_entry
            }
        except Exception as e:
            log_entry["unexpectedError"] = str(e)
            log_entry["finalOutcome"] = "error"
            return {
                "success": False,
                "error": str(e),
                "logs": log_entry
            }
        finally:
            if server:
                try:
                    server.quit()
                except:
                    pass

    # Execute the entire email sending process with an overall timeout
    try:
        return await asyncio.wait_for(_send_email(), timeout=TOTAL_REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Overall request timed out after {TOTAL_REQUEST_TIMEOUT} seconds"
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
