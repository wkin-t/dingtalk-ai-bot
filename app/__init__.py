from flask import Flask

app = Flask(__name__)

# 导入路由以注册
from app import routes