from flask_mail import Mail, Message
from flask import current_app

mail = Mail()

def init_mail(app):
    app.config['MAIL_SERVER'] = current_app.config.get("MAIL_SERVER")
    app.config['MAIL_PORT'] = int(current_app.config.get("MAIL_PORT", 587))
    app.config['MAIL_USE_TLS'] = current_app.config.get("MAIL_USE_TLS", True)
    app.config['MAIL_USERNAME'] = current_app.config.get("MAIL_USERNAME")
    app.config['MAIL_PASSWORD'] = current_app.config.get("MAIL_PASSWORD")
    app.config['MAIL_DEFAULT_SENDER'] = current_app.config.get("MAIL_DEFAULT_SENDER")
    mail.init_app(app)

def send_email(to, subject, body):
    """Generic email sender"""
    msg = Message(subject, recipients=[to], body=body)
    mail.send(msg)
