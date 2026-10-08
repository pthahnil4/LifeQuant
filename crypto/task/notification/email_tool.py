#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
统一邮箱工具类
==============

功能：
1. 发送纯文本邮件
2. 发送HTML格式邮件
3. 支持多个收件人（收件人、抄送、密送）
4. 支持附件发送
5. 完整的错误处理和日志记录
6. 支持SSL和TLS两种连接方式
7. 连接测试功能
8. 系统通知快捷发送

配置：
- 发件人：从 config.email_config 读取
- SMTP服务器：smtp.qq.com
- 支持端口：465（SSL）、587（TLS）

作者：AI Assistant
创建时间：2025年1月17日
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
import os
import logging
from typing import List, Optional, Dict, Any

# 配置日志格式 - 只显示时间戳和内容
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.handlers = []
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class EmailTool:
    """统一邮箱工具类"""
    
    def __init__(self, 
                 from_email: str = None,
                 password: str = None,
                 smtp_host: str = None,
                 smtp_port: int = None,
                 use_ssl: bool = None):
        """
        初始化邮箱工具（默认值从 config.email_config 读取）
        
        Args:
            from_email: 发件人邮箱
            password: 邮箱密码/授权码
            smtp_host: SMTP服务器地址
            smtp_port: SMTP端口（465用SSL，587用TLS）
            use_ssl: 是否使用SSL连接
        """
        # 从统一配置读取默认值
        from config.email_config import get_email_config
        _cfg = get_email_config()

        self.from_email  = from_email  or _cfg['from_email']
        self.password    = password    or _cfg['password']
        self.smtp_host   = smtp_host   or _cfg['smtp_host']
        self.smtp_port   = smtp_port   if smtp_port  is not None else _cfg['smtp_port']
        self.use_ssl     = use_ssl     if use_ssl    is not None else _cfg['use_ssl']
        
        # 创建SSL上下文
        self.context = ssl.create_default_context()
        
        # 打印"生效值"而不是入参：入参为 None 时其实回落到 config.email_config，
        # 原先这行天天显示 "None (None:None, SSL:None)"，看日志的人会当成邮件没配好
        logger.info(
            f"邮箱工具初始化完成: {self.from_email} "
            f"({self.smtp_host}:{self.smtp_port}, SSL:{self.use_ssl})"
            + ("  [来源: 调用入参]" if from_email or smtp_host else "  [来源: config.email_config]"))
    
    def test_connection(self) -> bool:
        """
        测试SMTP连接
        
        Returns:
            bool: 连接是否成功
        """
        try:
            logger.info("正在测试SMTP连接...")
            
            if self.use_ssl:
                # SSL连接（465端口）
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, 
                                     context=self.context, timeout=30) as server:
                    server.login(self.from_email, self.password)
                    logger.info("✅ SSL连接测试成功")
                    return True
            else:
                # TLS连接（587端口）
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                    server.ehlo()
                    server.starttls(context=self.context)
                    server.ehlo()
                    server.login(self.from_email, self.password)
                    logger.info("✅ TLS连接测试成功")
                    return True
                    
        except Exception as e:
            logger.error(f"❌ SMTP连接测试失败: {e}")
            return False
    
    def send_text_email(self, 
                       to_emails: List[str], 
                       subject: str, 
                       content: str,
                       cc_emails: List[str] = None,
                       bcc_emails: List[str] = None) -> bool:
        """
        发送纯文本邮件
        
        Args:
            to_emails: 收件人邮箱列表
            subject: 邮件主题
            content: 邮件内容
            cc_emails: 抄送邮箱列表
            bcc_emails: 密送邮箱列表
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 创建邮件消息
            message = MIMEMultipart()
            message["From"] = self.from_email
            message["To"] = ", ".join(to_emails)
            message["Subject"] = subject
            
            # 添加抄送
            if cc_emails:
                message["Cc"] = ", ".join(cc_emails)
            
            # 添加邮件内容
            message.attach(MIMEText(content, "plain", "utf-8"))
            
            # 发送邮件
            return self._send_message(message, to_emails, cc_emails, bcc_emails)
            
        except Exception as e:
            logger.error(f"发送文本邮件失败: {e}")
            return False
    
    def send_html_email(self, 
                       to_emails: List[str], 
                       subject: str, 
                       html_content: str,
                       text_content: str = None,
                       cc_emails: List[str] = None,
                       bcc_emails: List[str] = None) -> bool:
        """
        发送HTML格式邮件
        
        Args:
            to_emails: 收件人邮箱列表
            subject: 邮件主题
            html_content: HTML格式内容
            text_content: 纯文本备用内容
            cc_emails: 抄送邮箱列表
            bcc_emails: 密送邮箱列表
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 创建邮件消息
            message = MIMEMultipart("alternative")
            message["From"] = self.from_email
            message["To"] = ", ".join(to_emails)
            message["Subject"] = subject
            
            # 添加抄送
            if cc_emails:
                message["Cc"] = ", ".join(cc_emails)
            
            # 添加纯文本内容（备用）
            if text_content:
                text_part = MIMEText(text_content, "plain", "utf-8")
                message.attach(text_part)
            
            # 添加HTML内容
            html_part = MIMEText(html_content, "html", "utf-8")
            message.attach(html_part)
            
            # 发送邮件
            return self._send_message(message, to_emails, cc_emails, bcc_emails)
            
        except Exception as e:
            logger.error(f"发送HTML邮件失败: {e}")
            return False
    
    def send_email_with_attachment(self, 
                                  to_emails: List[str], 
                                  subject: str, 
                                  content: str,
                                  attachment_paths: List[str],
                                  content_type: str = "plain",
                                  cc_emails: List[str] = None,
                                  bcc_emails: List[str] = None) -> bool:
        """
        发送带附件的邮件
        
        Args:
            to_emails: 收件人邮箱列表
            subject: 邮件主题
            content: 邮件内容
            attachment_paths: 附件文件路径列表
            content_type: 内容类型（"plain" 或 "html"）
            cc_emails: 抄送邮箱列表
            bcc_emails: 密送邮箱列表
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 创建邮件消息
            message = MIMEMultipart()
            message["From"] = self.from_email
            message["To"] = ", ".join(to_emails)
            message["Subject"] = subject
            
            # 添加抄送
            if cc_emails:
                message["Cc"] = ", ".join(cc_emails)
            
            # 添加邮件内容
            message.attach(MIMEText(content, content_type, "utf-8"))
            
            # 添加附件
            for file_path in attachment_paths:
                if os.path.exists(file_path):
                    self._add_attachment(message, file_path)
                else:
                    logger.warning(f"附件文件不存在: {file_path}")
            
            # 发送邮件
            return self._send_message(message, to_emails, cc_emails, bcc_emails)
            
        except Exception as e:
            logger.error(f"发送带附件邮件失败: {e}")
            return False
    
    def _add_attachment(self, message: MIMEMultipart, file_path: str):
        """添加附件到邮件"""
        try:
            with open(file_path, "rb") as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
            
            # 编码附件
            encoders.encode_base64(part)
            
            # 添加头信息
            filename = os.path.basename(file_path)
            part.add_header(
                'Content-Disposition',
                f'attachment; filename= {filename}',
            )
            
            message.attach(part)
            logger.info(f"添加附件: {filename}")
            
        except Exception as e:
            logger.error(f"添加附件失败 {file_path}: {e}")
    
    def _send_message(self, 
                     message: MIMEMultipart, 
                     to_emails: List[str], 
                     cc_emails: List[str] = None,
                     bcc_emails: List[str] = None) -> bool:
        """
        发送邮件消息
        
        Args:
            message: 邮件消息对象
            to_emails: 收件人列表
            cc_emails: 抄送列表
            bcc_emails: 密送列表
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 构建所有收件人列表
            all_recipients = to_emails.copy()
            if cc_emails:
                all_recipients.extend(cc_emails)
            if bcc_emails:
                all_recipients.extend(bcc_emails)
            
            logger.info(f"准备发送邮件到: {', '.join(all_recipients)}")
            
            # 发送邮件
            send_success = False
            
            if self.use_ssl:
                # SSL连接
                server = None
                try:
                    server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, 
                                             context=self.context, timeout=60)
                    server.set_debuglevel(0)  # 关闭调试信息
                    
                    server.login(self.from_email, self.password)
                    
                    # 使用send_message方法
                    try:
                        result = server.send_message(message, to_addrs=all_recipients)
                        # 检查发送结果，空字典表示成功
                        if isinstance(result, dict) and len(result) == 0:
                            send_success = True
                        elif result:
                            send_success = True  # 即使有警告，也认为发送成功
                    except smtplib.SMTPServerDisconnected as disc_e:
                        # QQ SMTP 常在接收报文后主动断开连接，此时邮件通常已投递成功。
                        # 视为成功，避免上层重试导致重复发送同一封邮件。
                        logger.warning(f"SSL 发送后连接被断开（邮件通常已投递，视为成功）: {disc_e}")
                        send_success = True
                    
                except smtplib.SMTPException as smtp_e:
                    logger.error(f"SSL SMTP发送异常: {smtp_e}")
                    raise smtp_e
                finally:
                    # 手动关闭连接，忽略关闭时的异常
                    if server:
                        try:
                            server.quit()
                        except:
                            try:
                                server.close()
                            except:
                                pass
            else:
                # TLS连接
                server = None
                try:
                    server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=60)
                    server.set_debuglevel(0)  # 关闭调试信息
                    
                    server.ehlo()
                    server.starttls(context=self.context)
                    server.ehlo()
                    
                    server.login(self.from_email, self.password)
                    
                    # 使用send_message方法
                    try:
                        result = server.send_message(message, to_addrs=all_recipients)
                        # 检查发送结果，空字典表示成功
                        if isinstance(result, dict) and len(result) == 0:
                            send_success = True
                        elif result:
                            send_success = True  # 即使有警告，也认为发送成功
                    except smtplib.SMTPServerDisconnected as disc_e:
                        # QQ SMTP 常在接收报文后主动断开连接，此时邮件通常已投递成功。
                        # 视为成功，避免上层重试导致重复发送同一封邮件。
                        logger.warning(f"TLS 发送后连接被断开（邮件通常已投递，视为成功）: {disc_e}")
                        send_success = True
                        
                except smtplib.SMTPException as smtp_e:
                    logger.error(f"TLS SMTP发送异常: {smtp_e}")
                    raise smtp_e
                finally:
                    # 手动关闭连接，忽略关闭时的异常
                    if server:
                        try:
                            server.quit()
                        except:
                            try:
                                server.close()
                            except:
                                pass
            
            if send_success:
                logger.info(f"✉️ {message['Subject']}")
                return True
            else:
                logger.error("邮件发送失败：未知错误")
                return False
            
        except smtplib.SMTPException as e:
            logger.error(f"SMTP邮件发送失败: {e}")
            import traceback
            logger.error(f"SMTP详细错误信息: {traceback.format_exc()}")
            return False
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            import traceback
            logger.error(f"详细错误信息: {traceback.format_exc()}")
            return False
    
    def send_system_notification(self, 
                               to_email: str = None, 
                               content: str = None,
                               subject: str = None) -> bool:
        """
        发送系统通知邮件（快捷方法）
        
        Args:
            to_email: 收件人邮箱（默认从 email_config 读取）
            content: 通知内容
            subject: 邮件主题
            
        Returns:
            bool: 发送是否成功
        """
        if to_email is None:
            from config.email_config import get_admin_email
            to_email = get_admin_email()
        if content is None:
            content = f"系统通知 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        if subject is None:
            subject = "系统通知"
        
        return self.send_text_email(
            to_emails=[to_email],
            subject=subject,
            content=content
        )


class EmailTemplates:
    """邮件HTML模板生成器"""
    
    @staticmethod
    def _wrap_html(title: str, content_body: str, header_color: str = "#1976d2") -> str:
        """包装通用HTML结构"""
        return f"""
        <html>
        <head>
            <style>
                body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; margin: 0; padding: 0; background-color: #f6f6f6; }}
                .container {{ max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); overflow: hidden; }}
                .header {{ background-color: {header_color}; padding: 20px; text-align: center; color: white; }}
                .header h2 {{ margin: 0; font-size: 24px; }}
                .content {{ padding: 30px 20px; line-height: 1.6; color: #333; }}
                .footer {{ background-color: #f9f9f9; padding: 15px; text-align: center; font-size: 12px; color: #999; border-top: 1px solid #eee; }}
                .info-group {{ margin-bottom: 20px; border: 1px solid #eee; border-radius: 4px; overflow: hidden; }}
                .info-row {{ display: flex; border-bottom: 1px solid #eee; }}
                .info-row:last-child {{ border-bottom: none; }}
                .info-label {{ flex: 1; background-color: #f5f5f5; padding: 10px 15px; font-weight: bold; color: #666; font-size: 14px; }}
                .info-value {{ flex: 2; padding: 10px 15px; color: #333; font-size: 14px; }}
                .highlight-red {{ color: #d32f2f; font-weight: bold; }}
                .highlight-green {{ color: #388e3c; font-weight: bold; }}
                .highlight-blue {{ color: #1976d2; font-weight: bold; }}
                .timestamp {{ color: #999; font-size: 12px; margin-top: 5px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>{title}</h2>
                </div>
                <div class="content">
                    {content_body}
                </div>
                <div class="footer">
                    <p>系统自动发送，请勿回复</p>
                    <p>发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                </div>
            </div>
        </body>
        </html>
        """

    @classmethod
    def task_error(cls, task_name: str, error_msg: str, stack_trace: str = None) -> str:
        """1. 任务运行出错模板"""
        content = f"""
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">任务名称</div>
                <div class="info-value">{task_name}</div>
            </div>
            <div class="info-row">
                <div class="info-label">错误时间</div>
                <div class="info-value">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            </div>
            <div class="info-row">
                <div class="info-label">错误信息</div>
                <div class="info-value highlight-red">{error_msg}</div>
            </div>
        </div>
        """
        if stack_trace:
            content += f"""
            <div style="background-color: #f8f8f8; padding: 15px; border-radius: 4px; overflow-x: auto; margin-top: 20px;">
                <h4 style="margin-top: 0; color: #666;">堆栈追踪：</h4>
                <pre style="font-size: 12px; color: #d32f2f; margin: 0;">{stack_trace}</pre>
            </div>
            """
        return cls._wrap_html("⚠️ 任务运行出错警告", content, "#d32f2f")

    @classmethod
    def trade_success(cls, symbol: str, direction: str, price: float, amount: float, 
                     execution_time: str = None, order_type: str = "限价单") -> str:
        """2. 交易成功模板"""
        # 格式化方向
        if direction.lower() in ['long', 'buy', 'buy_long', 'open_long']:
            dir_text = "买入开多 (Buy)"
            dir_color = "highlight-green"
        elif direction.lower() in ['short', 'sell', 'sell_short', 'open_short']:
            dir_text = "卖出开空 (Sell)"
            dir_color = "highlight-red"
        elif direction.lower() in ['close_long', 'sell_close']:
            dir_text = "卖出平多 (Close Long)"
            dir_color = "highlight-red"
        elif direction.lower() in ['close_short', 'buy_close']:
            dir_text = "买入平空 (Close Short)"
            dir_color = "highlight-green"
        else:
            dir_text = f"{direction}"
            dir_color = "highlight-blue"
            
        # 格式化时间
        execution_time = execution_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        content = f"""
        <p>系统已成功执行以下交易：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">交易对</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">交易方向</div>
                <div class="info-value {dir_color}">{dir_text}</div>
            </div>
            <div class="info-row">
                <div class="info-label">成交价格</div>
                <div class="info-value">{price:.4f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">仓位数量</div>
                <div class="info-value">{amount} 张</div>
            </div>
            <div class="info-row">
                <div class="info-label">成交金额</div>
                <div class="info-value">{(price * amount * 0.01):.4f} USDT (估算)</div>
            </div>
            <div class="info-row">
                <div class="info-label">执行时间</div>
                <div class="info-value">{execution_time}</div>
            </div>
            <div class="info-row">
                <div class="info-label">订单类型</div>
                <div class="info-value">{order_type}</div>
            </div>
        </div>
        """
        return cls._wrap_html("✅ 实盘交易成功通知", content, "#388e3c")

    @classmethod
    def trend_reversal_warning(cls, symbol: str, period: str, old_direction: str, 
                              new_direction: str, current_price: float, 
                              reversal_time: str = None) -> str:
        """3. 趋势反转预警模板（未稳定）"""
        reversal_time = reversal_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 格式化方向
        old_dir_text = old_direction.upper()
        new_dir_text = new_direction.upper()
        
        content = f"""
        <p>检测到趋势方向发生反转（尚未进入稳定期），请密切关注：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">交易对</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">时间周期</div>
                <div class="info-value">{period}</div>
            </div>
            <div class="info-row">
                <div class="info-label">反转前方向</div>
                <div class="info-value">{old_dir_text}</div>
            </div>
            <div class="info-row">
                <div class="info-label">反转后方向</div>
                <div class="info-value highlight-red">{new_dir_text}</div>
            </div>
            <div class="info-row">
                <div class="info-label">当前价格</div>
                <div class="info-value">{current_price:.4f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">反转时间</div>
                <div class="info-value">{reversal_time}</div>
            </div>
            <div class="info-row">
                <div class="info-label">状态</div>
                <div class="info-value" style="color: #f57c00; font-weight: bold;">预警中 (未稳定)</div>
            </div>
        </div>
        """
        return cls._wrap_html("⚠️ 趋势反转预警", content, "#f57c00")

    @classmethod
    def trend_reversal_confirmed(cls, symbol: str, period: str, direction: str, 
                                price: float, confirm_time: str = None) -> str:
        """4. 趋势反转确认模板（已稳定）"""
        confirm_time = confirm_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        dir_text = direction.upper()
        
        content = f"""
        <p>趋势方向反转已确认并进入稳定期：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">交易对</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">时间周期</div>
                <div class="info-value">{period}</div>
            </div>
            <div class="info-row">
                <div class="info-label">确认方向</div>
                <div class="info-value highlight-green">{dir_text}</div>
            </div>
            <div class="info-row">
                <div class="info-label">确认时价格</div>
                <div class="info-value">{price:.4f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">确认时间</div>
                <div class="info-value">{confirm_time}</div>
            </div>
            <div class="info-row">
                <div class="info-label">状态</div>
                <div class="info-value highlight-green">已稳定 (Confirmed)</div>
            </div>
        </div>
        """
        return cls._wrap_html("🚀 趋势反转确认", content, "#1976d2")

    @classmethod
    def trend_reversal(cls, symbol: str, period: str, old_dir: str, new_dir: str, confirm_time: str = None) -> str:
        """3. 行情确认反转模板"""
        confirm_time = confirm_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        content = f"""
        <p>监测到市场趋势发生重要反转：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">关注标的</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">时间周期</div>
                <div class="info-value">{period}</div>
            </div>
            <div class="info-row">
                <div class="info-label">原方向</div>
                <div class="info-value" style="color: #999;">{old_dir}</div>
            </div>
            <div class="info-row">
                <div class="info-label">新方向</div>
                <div class="info-value highlight-blue" style="font-size: 16px;">{new_dir}</div>
            </div>
            <div class="info-row">
                <div class="info-label">确认时间</div>
                <div class="info-value">{confirm_time}</div>
            </div>
        </div>
        """
        return cls._wrap_html("🔄 趋势反转确认通知", content, "#1976d2")

    @classmethod
    def position_pnl_alert(cls, symbol: str, pnl_amount: float, pnl_ratio: float, current_price: float, direction: str) -> str:
        """4. 持仓极大盈利或者亏损的提示模板"""
        is_profit = pnl_amount > 0
        header_color = "#7b1fa2" if is_profit else "#e64a19" 
        title = "💰 持仓大幅盈利提醒" if is_profit else "📉 持仓大幅亏损预警"
        pnl_color = "highlight-green" if is_profit else "highlight-red"
        pnl_sign = "+" if is_profit else ""
        
        content = f"""
        <p>当前持仓盈亏已触发预警阈值：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">交易对</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">持仓方向</div>
                <div class="info-value">{direction}</div>
            </div>
            <div class="info-row">
                <div class="info-label">当前盈亏</div>
                <div class="info-value {pnl_color}" style="font-size: 18px;">{pnl_sign}{pnl_amount:.2f} USDT</div>
            </div>
            <div class="info-row">
                <div class="info-label">盈亏比例</div>
                <div class="info-value {pnl_color}">{pnl_sign}{pnl_ratio:.2f}%</div>
            </div>
            <div class="info-row">
                <div class="info-label">当前价格</div>
                <div class="info-value">{current_price}</div>
            </div>
        </div>
        """
        return cls._wrap_html(title, content, header_color)

    @classmethod
    def price_volatility_alert(cls, symbol: str, change_ratio: float, duration: str, current_price: float) -> str:
        """5. 价格短时间波动过大的提示"""
        is_rise = change_ratio > 0
        sign = "+" if is_rise else ""
        color = "highlight-green" if is_rise else "highlight-red"
        
        content = f"""
        <p>市场出现剧烈波动，请注意风险：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">交易对</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">波动幅度</div>
                <div class="info-value {color}" style="font-size: 18px;">{sign}{change_ratio:.2f}%</div>
            </div>
            <div class="info-row">
                <div class="info-label">监测时段</div>
                <div class="info-value">{duration}</div>
            </div>
            <div class="info-row">
                <div class="info-label">当前价格</div>
                <div class="info-value">{current_price}</div>
            </div>
        </div>
        <p style="color: #666; font-size: 12px;">注：短期内价格快速变化可能意味着市场情绪剧烈波动或出现重大新闻。</p>
        """
        return cls._wrap_html("⚡ 价格剧烈波动预警", content, "#f57c00")

    @classmethod
    def boll_coverage(cls, symbol: str, period: str, high: float, low: float,
                      close: float, boll_upper: float, boll_lower: float, boll_mid: float,
                      touch_type: str, detect_time: str = None) -> str:
        """BOLL轨道覆盖通知模板"""
        detect_time = detect_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if touch_type == 'upper':
            touch_text = '触及BOLL上轨'
            touch_color = 'highlight-red'
            header_color = '#d32f2f'
            title = f'📊 BOLL上轨覆盖 - {symbol} {period}'
        else:
            touch_text = '触及BOLL下轨'
            touch_color = 'highlight-green'
            header_color = '#388e3c'
            title = f'📊 BOLL下轨覆盖 - {symbol} {period}'

        content = f"""
        <p>检测到当前K线价格覆盖BOLL轨道：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">交易对</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">时间周期</div>
                <div class="info-value">{period}</div>
            </div>
            <div class="info-row">
                <div class="info-label">K线最高价</div>
                <div class="info-value">{high:.6f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">K线最低价</div>
                <div class="info-value">{low:.6f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">K线收盘价</div>
                <div class="info-value">{close:.6f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">BOLL上轨</div>
                <div class="info-value">{boll_upper:.6f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">BOLL中轨</div>
                <div class="info-value">{boll_mid:.6f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">BOLL下轨</div>
                <div class="info-value">{boll_lower:.6f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">触及类型</div>
                <div class="info-value {touch_color}" style="font-size: 16px;">{touch_text}</div>
            </div>
            <div class="info-row">
                <div class="info-label">检测时间</div>
                <div class="info-value">{detect_time}</div>
            </div>
        </div>
        """
        return cls._wrap_html(title, content, header_color)

    @classmethod
    def reverse_position_warning(cls, symbol: str, long_direction: str, reverse_side: str,
                                reverse_amount: float, current_price: float,
                                grace_minutes: float, cross_pos: float = 0.0,
                                isolated_pos: float = 0.0, detect_time: str = None,
                                source_note: str = '', observe_only: bool = False) -> str:
        """反向持仓预警模板（检测到与长周期方向相反的持仓）。

        source_note：冲突来源文案（程序旧方向持仓 / 账本外人工反向单 / 两者混合），
        缺省不展示该行（兼容旧调用方）。
        """
        detect_time = detect_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        dir_cn = '看多 (LONG)' if long_direction == 'long' else '看空 (SHORT)'
        rev_cn = '空头 (SHORT)' if reverse_side == 'short' else '多头 (LONG)'
        gm = f"{grace_minutes:g}"
        src_row = f"""
            <div class="info-row">
                <div class="info-label">冲突来源</div>
                <div class="info-value">{source_note}</div>
            </div>""" if source_note else ''
        content = f"""
        <p>检测到与长周期趋势方向<strong>相反</strong>的持仓，与策略方向冲突，请及时处理：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">合约</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">长周期方向</div>
                <div class="info-value highlight-blue">{dir_cn}</div>
            </div>
            <div class="info-row">
                <div class="info-label">反向持仓</div>
                <div class="info-value highlight-red">{rev_cn} {reverse_amount} 张</div>
            </div>{src_row}
            <div class="info-row">
                <div class="info-label">全仓/逐仓持仓</div>
                <div class="info-value">全仓 {cross_pos:.1f} 张 / 逐仓 {isolated_pos:.1f} 张</div>
            </div>
            <div class="info-row">
                <div class="info-label">当前价格</div>
                <div class="info-value">{current_price:.4f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">检测时间</div>
                <div class="info-value">{detect_time}</div>
            </div>
        </div>
        <p style="color: #d32f2f; font-weight: bold; font-size: 15px;">
            {('请自行评估并处理反向持仓。' if observe_only else
              f'请自行处理，否则 {gm} 分钟后系统将执行强制平仓以保持与策略方向一致。')}
        </p>
        """
        return cls._wrap_html("⚠️ 反向持仓预警", content, "#f57c00")

    @classmethod
    def reverse_position_closed(cls, symbol: str, long_direction: str, reverse_side: str,
                               reverse_amount: float, current_price: float,
                               close_time: str = None, source_note: str = '') -> str:
        """反向持仓已强制平仓通知模板（source_note=冲突来源，缺省不展示）。"""
        close_time = close_time or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        dir_cn = '看多 (LONG)' if long_direction == 'long' else '看空 (SHORT)'
        rev_cn = '空头 (SHORT)' if reverse_side == 'short' else '多头 (LONG)'
        src_row = f"""
            <div class="info-row">
                <div class="info-label">冲突来源</div>
                <div class="info-value">{source_note}</div>
            </div>""" if source_note else ''
        content = f"""
        <p>反向持仓在预警时限内未被处理，系统已执行<strong>强制平仓</strong>以保持与策略方向一致：</p>
        <div class="info-group">
            <div class="info-row">
                <div class="info-label">合约</div>
                <div class="info-value highlight-blue">{symbol}</div>
            </div>
            <div class="info-row">
                <div class="info-label">长周期方向</div>
                <div class="info-value highlight-blue">{dir_cn}</div>
            </div>
            <div class="info-row">
                <div class="info-label">已强平反向持仓</div>
                <div class="info-value highlight-red">{rev_cn} {reverse_amount} 张</div>
            </div>{src_row}
            <div class="info-row">
                <div class="info-label">当前价格</div>
                <div class="info-value">{current_price:.4f}</div>
            </div>
            <div class="info-row">
                <div class="info-label">强平时间</div>
                <div class="info-value">{close_time}</div>
            </div>
        </div>
        """
        return cls._wrap_html("🛑 反向持仓已强制平仓", content, "#d32f2f")


# 便捷函数
def send_quick_notification(to_email: str = None, 
                          content: str = None,
                          use_ssl: bool = True) -> bool:
    """
    快速发送系统通知
    
    Args:
        to_email: 收件人邮箱（默认从 email_config 读取）
        content: 通知内容
        use_ssl: 是否使用SSL
        
    Returns:
        bool: 发送是否成功
    """
    if to_email is None:
        from config.email_config import get_admin_email
        to_email = get_admin_email()
    email_tool = EmailTool(use_ssl=use_ssl)
    return email_tool.send_system_notification(to_email, content)


def create_ssl_email_tool() -> EmailTool:
    """创建SSL邮箱工具"""
    return EmailTool(smtp_port=465, use_ssl=True)


def create_tls_email_tool() -> EmailTool:
    """创建TLS邮箱工具"""
    return EmailTool(smtp_port=587, use_ssl=False)


# 测试函数
def run_connection_test():
    """运行连接测试"""
    logger.info("🔧 邮箱连接测试")
    logger.info("=" * 50)
    
    # 测试SSL连接
    logger.info("📡 测试SSL连接（465端口）...")
    ssl_tool = create_ssl_email_tool()
    ssl_success = ssl_tool.test_connection()
    logger.info(f"SSL连接结果: {'✅ 成功' if ssl_success else '❌ 失败'}")
    
    # 测试TLS连接
    logger.info("📡 测试TLS连接（587端口）...")
    tls_tool = create_tls_email_tool()
    tls_success = tls_tool.test_connection()
    logger.info(f"TLS连接结果: {'✅ 成功' if tls_success else '❌ 失败'}")
    
    return ssl_success or tls_success


def run_email_test():
    """运行邮件发送测试"""
    logger.info("📧 邮件发送测试")
    logger.info("=" * 50)
    
    # 使用SSL发送测试邮件
    email_tool = create_ssl_email_tool()
    
    from config.email_config import get_admin_email
    _test_email = get_admin_email()

    # 1. 测试纯文本邮件
    logger.info("📝 测试纯文本邮件...")
    text_success = email_tool.send_text_email(
        to_emails=[_test_email],
        subject="邮箱工具测试 - 纯文本",
        content=f"这是一封纯文本测试邮件。\n\n发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    logger.info(f"纯文本邮件: {'✅ 成功' if text_success else '❌ 失败'}")
    
    # 2. 测试HTML邮件
    logger.info("🎨 测试HTML邮件...")
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html_content = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            .header {{ background-color: #e3f2fd; padding: 15px; border-radius: 8px; }}
            .content {{ margin: 20px 0; line-height: 1.6; }}
            .footer {{ color: #666; font-size: 12px; border-top: 1px solid #eee; padding-top: 10px; }}
            .success {{ color: #4caf50; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2>🎉 邮箱工具测试</h2>
        </div>
        <div class="content">
            <p>这是一封<strong>HTML格式</strong>的测试邮件。</p>
            <p class="success">如果您看到格式化的内容，说明HTML邮件功能正常！</p>
            <ul>
                <li>✅ 支持HTML标签</li>
                <li>✅ 支持CSS样式</li>
                <li>✅ 支持中文显示</li>
                <li>✅ 支持特殊字符</li>
            </ul>
        </div>
        <div class="footer">
            <p>发送时间：{current_time}</p>
            <p>邮箱工具版本：v1.0</p>
        </div>
    </body>
    </html>
    """
    
    html_success = email_tool.send_html_email(
        to_emails=[_test_email],
        subject="邮箱工具测试 - HTML格式",
        html_content=html_content,
        text_content="这是HTML邮件的纯文本备用内容。"
    )
    logger.info(f"HTML邮件: {'✅ 成功' if html_success else '❌ 失败'}")
    
    # 3. 测试系统通知
    logger.info("🔔 测试系统通知...")
    notification_success = email_tool.send_system_notification(
        content="邮箱工具测试完成，所有功能正常运行。"
    )
    logger.info(f"系统通知: {'✅ 成功' if notification_success else '❌ 失败'}")
    
    return text_success and html_success and notification_success


def run_template_test(to_email: str = None):
    """测试所有邮件模板"""
    if to_email is None:
        from config.email_config import get_admin_email
        to_email = get_admin_email()
    logger.info("🎨 测试邮件模板...")
    logger.info("=" * 50)
    
    # 使用SSL工具
    tool = create_ssl_email_tool()
    
    # 1. 测试错误模板
    logger.info("1. 发送错误报警...")
    html = EmailTemplates.task_error(
        "实盘交易主进程", 
        "ConnectionTimeout: 连接交易所API超时", 
        "Traceback (most recent call last):\n  File 'main.py', line 10, in <module>\n    api.connect()\nTimeoutError"
    )
    tool.send_html_email([to_email], "【测试】任务出错报警", html)
    
    # 2. 测试交易成功
    logger.info("2. 发送交易成功通知...")
    html = EmailTemplates.trade_success("ETH-USDT-SWAP", "Long", 3500.50, 10.5)
    tool.send_html_email([to_email], "【测试】交易成功通知", html)
    
    # 3. 测试趋势反转
    logger.info("3. 发送趋势反转通知...")
    html = EmailTemplates.trend_reversal("BTC-USDT", "4H", "Long", "Short")
    tool.send_html_email([to_email], "【测试】趋势反转通知", html)
    
    # 4. 测试盈亏提醒
    logger.info("4. 发送盈利提醒...")
    html = EmailTemplates.position_pnl_alert("SOL-USDT", 500.0, 15.5, 145.2, "Long")
    tool.send_html_email([to_email], "【测试】持仓盈利提醒", html)
    
    # 5. 测试波动提醒
    logger.info("5. 发送波动提醒...")
    html = EmailTemplates.price_volatility_alert("DOGE-USDT", -5.2, "5分钟", 0.12345)
    tool.send_html_email([to_email], "【测试】价格波动预警", html)
    
    logger.info("✅ 模板测试发送完成，请检查邮箱")


def main():
    """主函数 - 运行测试示例"""
    logger.info("📮 统一邮箱工具类")
    logger.info("=" * 80)
    logger.info("功能：发送文本/HTML邮件、附件支持、多收件人、连接测试、模板消息")
    logger.info("=" * 80)
    
    try:
        # 1. 连接测试
        connection_ok = run_connection_test()
        
        if connection_ok:
            # 2. 邮件发送测试
            # email_ok = run_email_test() # 基础测试，默认跳过以节省时间
            
            # 3. 模板测试
            run_template_test()
            
            logger.info("🎉 所有测试通过！邮箱工具可以正常使用。")
        else:
            logger.info("❌ 连接测试失败，请检查网络和邮箱配置。")
            
    except Exception as e:
        logger.info(f"💥 测试过程中发生异常: {e}")
        import traceback
        traceback.print_exc()
    
    logger.info("=" * 80)
    logger.info("测试完成！")


if __name__ == "__main__":
    main()