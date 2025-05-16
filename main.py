import os
import socket
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any
import aiosmtplib
import socks
import asyncio

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

async def get_proxy_ip(proxy_config: ProxyConfig) -> Dict[str, Any]:
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
        return {"success": True, "proxyIP": ip}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/send-email")
async def send_email(req: EmailRequest, request: Request):
    print("==== NEW REQUEST ====")
    print(f"SMTP Host: {req.smtpConfig.host}")
    print(f"SMTP Port: {req.smtpConfig.port}")
    print(f"SMTP Secure: {req.smtpConfig.secure}")
    print(f"SMTP User: {req.smtpConfig.auth.user}")
    print(f"Proxy Config: {req.proxyConfig}")

    log_entry = {
        "timestamp": str(asyncio.get_event_loop().time()),
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
    smtp_sock = None

    # Proxy setup
    if req.proxyConfig and req.proxyConfig.host:
        try:
            print("Trying to get proxy IP...")
            proxy_ip_info = await get_proxy_ip(req.proxyConfig)
            print(f"Proxy IP Info: {proxy_ip_info}")
            if proxy_ip_info["success"]:
                log_entry["afterProxyIp"] = proxy_ip_info["proxyIP"]
                use_proxy = True
                smtp_sock = socks.socksocket()
                smtp_sock.set_proxy(
                    socks.SOCKS5,
                    req.proxyConfig.host,
                    req.proxyConfig.port,
                    username=req.proxyConfig.username,
                    password=req.proxyConfig.password
                )
            else:
                log_entry["proxyError"] = proxy_ip_info["error"]
                log_entry["fallbackToDirect"] = True
                print(f"Proxy error: {proxy_ip_info['error']}")
        except Exception as e:
            log_entry["proxyError"] = str(e)
            log_entry["fallbackToDirect"] = True
            print(f"Proxy setup exception: {e}")

    # SMTP connection and authentication
    try:
        print("Setting up SMTP connection...")
        smtp = aiosmtplib.SMTP(
            hostname=req.smtpConfig.host,
            port=req.smtpConfig.port,
            timeout=10,
            sock=smtp_sock
        )
        await smtp.connect()
        print("Connected to SMTP server.")

        # Debug: Show logic for TLS/STARTTLS
        print(f"About to check for STARTTLS: secure={req.smtpConfig.secure}, port={req.smtpConfig.port}")
        if req.smtpConfig.secure and req.smtpConfig.port == 587:
            print("Calling STARTTLS...")
            await smtp.starttls()
            print("STARTTLS completed.")

        print("Logging in to SMTP server...")
        await smtp.login(req.smtpConfig.auth.user, req.smtpConfig.auth.password)
        print("SMTP login successful.")
        log_entry["connectionVerified"] = True
    except Exception as e:
        print(f"SMTP connection/auth error: {e}")
        log_entry["connectionVerified"] = False
        log_entry["verifyError"] = str(e)
        return {"success": False, "error": str(e), "logs": log_entry}

    # Send email
    try:
        from_addr = f'"{req.senderName}" <{req.senderEmail}>'
        to_addr = req.toEmail
        subject = req.subject
        code = req.code
        message = f"""\
From: {from_addr}
To: {to_addr}
Subject: {subject}
Content-Type: text/html

<p>Your verification code is: <strong>{code}</strong></p>
"""
        print("Sending email...")
        result = await smtp.sendmail(from_addr, [to_addr], message)
        await smtp.quit()
        print("Email sent and SMTP connection closed.")
        log_entry["smtpLogs"] = smtp_logs
        log_entry["connectionType"] = "proxy" if use_proxy else "direct"
        log_entry["finalOutcome"] = "success"
        return {
            "success": True,
            "messageId": result.get("message-id", ""),
            "logs": log_entry
        }
    except Exception as e:
        print(f"Error sending email: {e}")
        return {"success": False, "error": str(e), "logs": log_entry}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
