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
import logging
from contextlib import contextmanager

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
logger = logging.getLogger(__name__)

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

@contextmanager
def proxy_context(proxy_config: Optional[ProxyConfig] = None):
    """Context manager for handling proxy configuration cleanup"""
    original_socket = socket.socket
    try:
        if proxy_config:
            logger.info(f"Setting up proxy: {proxy_config.host}:{proxy_config.port}")
            socks.setdefaultproxy(
                socks.SOCKS5,
                proxy_config.host,
                proxy_config.port,
                True,
                proxy_config.username,
                proxy_config.password
            )
            socket.socket = socks.socksocket
        yield
    finally:
        # Always restore original socket
        socks.setdefaultproxy(None)
        socket.socket = original_socket

async def get_external_ip() -> Optional[str]:
    """Get external IP using multiple fallback services"""
    services = [
        ("icanhazip.com", 80),
        ("api.ipify.org", 80),
        ("ident.me", 80),
        ("myexternalip.com", 80)
    ]
    
    for host, port in services:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(b"GET / HTTP/1.1\r\nHost: " + host.encode() + b"\r\n\r\n")
                response = s.recv(4096).decode()
                if response:
                    # Extract IP from HTTP response
                    ip = response.split("\r\n\r\n")[-1].strip()
                    if ip and "." in ip:  # Basic IP validation
                        return ip
        except Exception as e:
            logger.warning(f"Failed to get IP from {host}: {str(e)}")
            continue
    
    return None

async def get_proxy_ip(smtp_host: str, smtp_port: int, proxy_config: ProxyConfig) -> dict:
    """Test proxy connection and get the proxy IP using multiple methods"""
    proxy_ip = None
    methods_used = []
    
    try:
        with proxy_context(proxy_config):
            # Method 1: Get IP from SMTP connection
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)
                    s.connect((smtp_host, smtp_port))
                    proxy_ip = s.getpeername()[0]
                    methods_used.append("smtp_peername")
            except Exception as e:
                logger.warning(f"SMTP peername method failed: {str(e)}")
            
            # Method 2: Get external IP from public services
            if not proxy_ip:
                external_ip = await get_external_ip()
                if external_ip:
                    proxy_ip = external_ip
                    methods_used.append("external_service")
            
            # Method 3: Get local socket address (less reliable)
            if not proxy_ip:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.connect(("8.8.8.8", 53))  # Google DNS
                        proxy_ip = s.getsockname()[0]
                        methods_used.append("local_socket")
                except:
                    pass
            
            if proxy_ip:
                return {
                    "success": True,
                    "proxyIP": proxy_ip,
                    "methodsUsed": methods_used
                }
            else:
                return {
                    "success": False,
                    "error": "Could not determine proxy IP using any method"
                }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

def create_smtp_connection(smtp_config: SMTPConfig, proxy_config: Optional[ProxyConfig] = None):
    """Create SMTP connection with optional proxy"""
    with proxy_context(proxy_config):
        server = smtplib.SMTP(smtp_config.host, smtp_config.port, timeout=20)
        server.set_debuglevel(1)
        return server

@app.post("/send-email")
async def send_email(req: EmailRequest, request: Request):
    # Initialize logging entry
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
            proxy_ip_info = await get_proxy_ip(req.smtpConfig.host, req.smtpConfig.port, req.proxyConfig)
            if proxy_ip_info["success"]:
                log_entry["afterProxyIp"] = proxy_ip_info["proxyIP"]
                log_entry["proxyMethodsUsed"] = proxy_ip_info.get("methodsUsed", [])
                use_proxy = True
                log_entry["proxyUsed"] = True
                log_entry["connectionType"] = "proxy"
                logger.info(f"Proxy connection established. IP: {proxy_ip_info['proxyIP']}")
            else:
                log_entry["proxyError"] = proxy_ip_info["error"]
                log_entry["fallbackToDirect"] = True
                log_entry["connectionType"] = "direct"
                logger.warning(f"Proxy failed, falling back to direct. Error: {proxy_ip_info['error']}")
        except Exception as e:
            log_entry["proxyError"] = str(e)
            log_entry["fallbackToDirect"] = True
            log_entry["connectionType"] = "direct"
            logger.error(f"Proxy setup error: {str(e)}")
    else:
        log_entry["noProxyConfigured"] = True
        log_entry["connectionType"] = "direct"
        logger.info("No proxy configured, using direct connection")

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
        server = None
        try:
            server = create_smtp_connection(req.smtpConfig, req.proxyConfig if use_proxy else None)
            server.ehlo()
            
            if req.smtpConfig.secure and req.smtpConfig.port == 587:
                server.starttls()
                server.ehlo()
            
            try:
                server.noop()
                log_entry["connectionVerified"] = True
                logger.info("SMTP connection verified")
            except Exception as verify_error:
                log_entry["connectionVerified"] = False
                log_entry["verifyError"] = str(verify_error)
                logger.warning(f"SMTP connection verification failed: {str(verify_error)}")
            
            server.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
            server.sendmail(from_addr, [to_addr], message)
            
            log_entry["finalOutcome"] = "success"
            log_entry["smtpSuccess"] = True
            logger.info(f"Email sent successfully to {to_addr}")
            
            return {
                "success": True,
                "messageId": message_id,
                "logs": log_entry
            }
        finally:
            if server:
                try:
                    server.quit()
                except Exception as e:
                    logger.warning(f"Error while quitting SMTP server: {str(e)}")
    except Exception as e:
        log_entry["smtpSuccess"] = False
        log_entry["smtpError"] = str(e)
        log_entry["finalOutcome"] = "error"
        logger.error(f"Email sending failed: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "logs": log_entry
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)), log_level="info")
