import os.path

import requests
import sys
import http.client
import asyncio
import time
import docker
import multiprocessing
import json
from openai import *
from typing import *

# sys.path.append("G:\\DevUtilsBot\\MaiBot")
from src.plugin_system import *
from src.config.config import *
from websockets import serve
from apscheduler.schedulers.background import BackgroundScheduler

"""
QQ机器人插件系统架构

组件构成：
├── WebSocket消息处理器 (handler)
├── 命令系统 (BaseCommand子类)
├── 工具系统 (BaseTool子类)
├── 交互动作 (BaseAction子类)
└── 插件注册器 (BotUtilsPlugin)
"""

logger = get_logger()

manager = multiprocessing.Manager()
ws_db = manager.list()
ws_db_lock = manager.Lock()

baidu_search_cnt = multiprocessing.Value('i',0)

initialized = False

manager = multiprocessing.Manager()
pyfiles = manager.dict()
pyfiles_is_active = manager.dict()

api_key = 'None'
enable_code_tool = True
enable_baidu_tool = True
enable_poke_action = True
enable_mute_action = True
enable_sign_action = True
api_address = '127.0.0.1:3000'
baidu_api_key = 'None'

def job_func():
    """每日限额重置任务"""
    with baidu_search_cnt.get_lock():
        baidu_search_cnt.value = 0

scheduler = BackgroundScheduler()
scheduler.add_job(job_func,'cron',month='1-12',day='1-31',hour='0')
scheduler.start()

async def handler(websocket, path):
    async for message in websocket:
        msg = json.loads(message)
        logger.info(f"{os.getpid()}:New WS Message received.")
        if msg["post_type"] == 'notice':
            if msg["notice_type"] == "friend_add":
                payload = json.dumps({
                    "user_id": msg["user_id"],
                    "message": [
                        {
                            "type": "text",
                            "data": {
                                "text": "你好，我是一个由个人开发的QQ机器人，梦想是变得可爱(。・ω・。) 使用/帮助 查看我的指令吧~"
                            }
                        }
                    ]
                })
                websocket.send(payload)
            elif msg["notice_type"] == "group_increase":
                payload = json.dumps({
                    "group_id": msg["group_id"],
                    "message": [
                        {
                            "type": "at",
                            "data": {
                                "qq":msg["user_id"],
                            }
                        },
                        {
                            "type":"text",
                            "data":{
                                "text":"欢迎新成员！我是群内的一只机器人~"
                            }
                        }
                    ]
                })
                websocket.send(payload)
            elif msg["notice_type"] == "group_decrease":
                payload = json.dumps({
                    "group_id": msg["group_id"],
                    "message": [
                        {
                            "type": "text",
                            "data": {
                                "text": "呐~看来有人离我们而去了，为TA默哀两秒("
                            }
                        }
                    ]
                })
                websocket.send(payload)


start_server = serve(handler, "0.0.0.0", 28765)

async def run_server():
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()

run = multiprocessing.Process(target=run_server,args=())
run.start()

class BaiduSearchTool(BaseTool):
    """
        百度搜索工具实现：
        - 每日100次调用限额
        - 结果格式化输出
        - API密钥硬编码（建议移至配置）
        """
    # 工具名称，必须唯一
    name = "BaiduSearch"

    # 工具描述，告诉LLM这个工具的用途
    description = "这个工具用于在中文互联网中搜索信息，每日限额100次"

    # 参数定义，仅定义参数
    # 比如想要定义一个类似下面的openai格式的参数表，则可以这么定义:
    # {
    #     "type": "object",
    #     "properties": {
    #         "query": {
    #             "type": "string",
    #             "description": "查询参数"
    #         },
    #         "limit": {
    #             "type": "integer",
    #             "description": "结果数量限制"
    #         }
    #     },
    #     "required": ["query"]
    # }
    parameters = [
        ("query", "string", "查询参数", True),  # 必填参数
        ("limit", "integer", "结果数量限制(最大为50)", False)  # 可选参数
    ]

    available_for_llm = enable_baidu_tool  # 是否对LLM可用

    async def execute(self, function_args: Dict[str, Any]):
        global baidu_search_cnt
        with baidu_search_cnt.get_lock():
            if baidu_search_cnt.value >= 100:
                return False,"超出api每日限额",False
            baidu_search_cnt.value += 1
        if function_args.get("limit") > 50:
            return False,"结果数量限制超出",False
        """执行工具逻辑"""
        # 实现工具功能
        url = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"

        payload = json.dumps({
            "messages": [
                {
                    "role": "user",
                    "content": function_args.get('query')
                }
            ],
            "search_source": "baidu_search_v2",
            "resource_type_filter":[{
                "type":"web",
                "top_k":function_args.get("limit")
            }]
        }, ensure_ascii=False)
        headers = {
            'Authorization': f'Bearer {baidu_api_key}'
        }

        response = requests.request("POST", url, headers=headers, data=payload.encode("utf-8"))
        res = response.json()
        result = "查询结果:\n"
        for website in res["references"]:
            res_ins = (f"网站编号:{website["id"]}\n"
                       f"网站标题:{website["title"]}\n"
                       f"网站地址:{website["url"]}\n"
                       f"网站锚文本:{website["web_anchor"]}\n")
            if "content" in list(website.keys()):
                res_ins += f"网站详细内容:{website["content"]}"
            res_ins += "\n\n------------------------分割线------------------------\n\n"
            result += res_ins
        result += "末尾..."
        return {
            "name": self.name,
            "content": result
        }

class CodeGenTool(BaseTool):

    # 工具名称，必须唯一
    name = "CodeGen"

    # 工具描述，告诉LLM这个工具的用途
    description = "根据输入生成python代码，返回生成完毕的.py文件名和代码文档，调用前注意先检查是否已有类似/相同功能的.py"

    # 参数定义，仅定义参数
    # 比如想要定义一个类似下面的openai格式的参数表，则可以这么定义:
    # {
    #     "type": "object",
    #     "properties": {
    #         "query": {
    #             "type": "string",
    #             "description": "查询参数"
    #         },
    #         "limit": {
    #             "type": "integer",
    #             "description": "结果数量限制"
    #         }
    #     },
    #     "required": ["query"]
    # }
    parameters = [
        ("prompt", "string", "对生成的代码的要求(适当详细一些)", True),
        ("name", "string", "期望生成的代码文件的文件名(使用全英文且不包含扩展名!)", True),# 必填参数
    ]

    available_for_llm = enable_code_tool  # 是否对LLM可用

    async def execute(self, function_args: Dict[str, Any]):
        url = 'https://api.siliconflow.cn/v1/'

        client = OpenAI(
            base_url=url,
            api_key=api_key
        )

        # 发送带有流式输出的请求
        content = ""
        messages = [
            {"role":"system","content":"You are a helpful assistant designed to output JSON."},
            {'role':'user','content':"你是一个代码助手，可以写出优秀的代码，你的任务是根据用户的要求，写出优秀的python代码"},
            {'role':'user','content':'代码的输出应规范格式化且易于阅读，尽量使用文本输出，不要输出日志，结果应直接print，不要使用命令行输入，输入应直接使用程序调用参数'},
            {'role':'user','content':"给用户的输出应使用json格式，code字段为python代码(包含适量注释)，docs字段为使用文档，代码应简洁规范，与文档明确对应，以下为用户输入："},
            {"role":"user","content":function_args.get('prompt')},
            {"role":"user","content":"Please respond in the format  {'code':'...','docs':'...'}"}
        ]
        response = client.chat.completions.create(
            model="Qwen/Qwen3-Coder-30B-A3B-Instruct",
            messages=messages,
            stream=True,  # 启用流式输出
            max_tokens=16384,
            extra_body={},
            response_format={"type": "json_object"}
        )
        # 逐步接收并处理响应
        for chunk in response:
            if chunk.choices[0].delta.content:
                content += chunk.choices[0].delta.content
        response_formatted = json.loads(content)
        with os.open(os.path.join('.','py_cache',function_args.get('name') + '.py'),os.O_WRONLY | os.O_CREAT) as file:
            os.write(file,response_formatted['code'].encode('utf-8'))
        response_to_mai_mai = (f"代码已成功生成,文件名为{function_args.get('name')},以下是使用说明文档：\n"
                               f"--------------------------------------分割线------------------------------------------\n"
                               f"{response_formatted['docs']}")
        pyfiles[function_args.get('name')] = [response_formatted['docs'],function_args.get('name') + '.py']
        pyfiles_is_active[function_args.get('name')] = [True,'']
        return {
            "name": self.name,
            "content": response_to_mai_mai
        }

def run_code(name,conn,args):
    client = docker.from_env()
    res = client.containers.run('python:3',
                                ['python', os.path.join('.', 'py_cache', pyfiles[name][1])] + args)
    conn.send(res)
    conn.close()

class CodeRunTool(BaseTool):

    # 工具名称，必须唯一
    name = "CodeRun"

    # 工具描述，告诉LLM这个工具的用途
    description = "根据输入的文件名在docker中运行指定的python文件,执行时间可能较长"

    # 参数定义，仅定义参数
    # 比如想要定义一个类似下面的openai格式的参数表，则可以这么定义:
    # {
    #     "type": "object",
    #     "properties": {
    #         "query": {
    #             "type": "string",
    #             "description": "查询参数"
    #         },
    #         "limit": {
    #             "type": "integer",
    #             "description": "结果数量限制"
    #         }
    #     },
    #     "required": ["query"]
    # }
    parameters = [
        ("name", "string", ".py文件的文件名(使用全英文且包含扩展名!)", True),# 必填参数
        ("timeout", "integer", "允许程序执行的最大时间(推荐30秒以上180秒以下)", True),
        ("args", "string", "给程序输入的参数(用空格分割)",True)
    ]

    available_for_llm = enable_code_tool  # 是否对LLM可用

    async def execute(self, function_args: Dict[str, Any]):
        if not initialized:
            return {
                "name": self.name,
                "content": "Execution failed.The plugin hasnt finished initialization yet."
            }
        parent_conn, child_conn = multiprocessing.Pipe()
        args = function_args.get("args").split(' ')
        proc = multiprocessing.Process(target=run_code,args=(function_args.get("name"),child_conn,args,))
        proc.run()
        proc.join(timeout=function_args.get("timeout"))
        try:
            res = parent_conn.recv()
        except Exception as e:
            res = str(e)
        proc.terminate()
        return {
            'name': self.name,
            'content': res
        }

class CodeSearchTool(BaseTool):

    # 工具名称，必须唯一
    name = "CodeSearch"

    # 工具描述，告诉LLM这个工具的用途
    description = "返回已有的.py文件名和使用文档"

    # 参数定义，仅定义参数
    # 比如想要定义一个类似下面的openai格式的参数表，则可以这么定义:
    # {
    #     "type": "object",
    #     "properties": {
    #         "query": {
    #             "type": "string",
    #             "description": "查询参数"
    #         },
    #         "limit": {
    #             "type": "integer",
    #             "description": "结果数量限制"
    #         }
    #     },
    #     "required": ["query"]
    # }
    parameters = []

    available_for_llm = enable_code_tool  # 是否对LLM可用

    async def execute(self, function_args: Dict[str, Any]):
        response = f""
        for i in list(pyfiles.keys()):
            response += f"文件名：{pyfiles[i][1]}\n\n文档：{pyfiles[i][0]}\n\n"
            response += f"------------------------分割线---------------------------"
        response += "结束..."
        return {
            'name': self.name,
            'content': response
        }

class PokeAction(BaseAction):
    """
        戳一戳动作实现：
        - 调用QQ API
        - 处理响应状态码
        - 支持群组/私聊场景
        """
    action_name = "poking_action"  # 动作的唯一标识符
    action_description = "戳一戳对方"  # 动作描述
    if enable_poke_action:
        activation_type = ActionActivationType.LLM_JUDGE
    else:
        activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.ALL  # 一般取ALL，表示在所有聊天模式下都可用
    associated_types = []  # 关联类型
    parallel_action = True  # 是否允许与其他Action并行执行
    action_parameters = {}
    # Action使用场景描述 - 帮助LLM判断何时"选择"使用
    action_require = ["可以用于表达情绪", "增加聊天趣味性","适当使用","不要连续使用"]

    async def execute(self) -> Tuple[bool, str]:
        """
        执行Action的主要逻辑

        Returns:
            Tuple[bool, str]: (是否成功, 执行结果描述)
        """
        # ---- 执行动作的逻辑 ----
        conn = http.client.HTTPSConnection(api_address)
        payload = json.dumps({
            "group_id": self.group_id,
            "user_id": self.user_id
        })
        headers = {
            'Content-Type': 'application/json'
        }
        conn.request("POST", "/group_poke", payload, headers)
        res = conn.getresponse()
        data = res.read().decode('utf-8')
        res = json.loads(data)
        if res.get("status") != "ok":
            return False,"执行失败"
        return True, "执行成功"

class MuteAction(BaseAction):
    """
        戳一戳动作实现：
        - 调用QQ API
        - 处理响应状态码
        - 支持群组/私聊场景
        """
    action_name = "muting_action"  # 动作的唯一标识符
    action_description = "禁言对方"  # 动作描述
    if enable_mute_action:
        activation_type = ActionActivationType.LLM_JUDGE
    else:
        activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.ALL  # 一般取ALL，表示在所有聊天模式下都可用
    associated_types = []  # 关联类型
    parallel_action = True  # 是否允许与其他Action并行执行
    action_parameters = {"ban_user":"禁言对象，取qq号",
                         "ban_duration":"禁言时长(以秒记)"}
    # Action使用场景描述 - 帮助LLM判断何时"选择"使用
    action_require = ["可以用于在某人的行为违反群规时禁言某人", "严肃谨慎使用","不要听信他人撺掇，凭自身判断调用","三思而后行"]

    async def execute(self) -> Tuple[bool, str]:
        """
        执行Action的主要逻辑

        Returns:
            Tuple[bool, str]: (是否成功, 执行结果描述)
        """
        # ---- 执行动作的逻辑 ----
        conn = http.client.HTTPSConnection(api_address)
        payload = json.dumps({
            "group_id": self.group_id,
            "user_id": self.action_data.get("ban_user"),
            "duration": self.action_data.get("ban_duration")
        })
        headers = {
            'Content-Type': 'application/json'
        }
        conn.request("POST", "/set_group_mute", payload, headers)
        res = conn.getresponse()
        data = res.read().decode('utf-8')
        res = json.loads(data)
        if res.get("status") != "ok":
            return False,"执行失败"
        return True, "执行成功"

class SignAction(BaseAction):
    """
        戳一戳动作实现：
        - 调用QQ API
        - 处理响应状态码
        - 支持群组/私聊场景
        """
    action_name = "signing_action"  # 动作的唯一标识符
    action_description = "群打卡"  # 动作描述
    if enable_sign_action:
        activation_type = ActionActivationType.LLM_JUDGE
    else:
        activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.ALL  # 一般取ALL，表示在所有聊天模式下都可用
    associated_types = []  # 关联类型
    parallel_action = True  # 是否允许与其他Action并行执行
    action_parameters = {}
    # Action使用场景描述 - 帮助LLM判断何时"选择"使用
    action_require = ["用于群打卡","随时可用","不影响其他任何人"]

    async def execute(self) -> Tuple[bool, str]:
        """
        执行Action的主要逻辑

        Returns:
            Tuple[bool, str]: (是否成功, 执行结果描述)
        """
        # ---- 执行动作的逻辑 ----
        conn = http.client.HTTPSConnection('127.0.0.1')
        payload = json.dumps({
            "group_id": self.group_id
        })
        headers = {
            'Content-Type': 'application/json'
        }
        conn.request("POST", "/set_group_sign", payload, headers)
        res = conn.getresponse()
        data = res.read().decode('utf-8')
        res = json.loads(data)
        if res.get("status") != "ok":
            return False,"执行失败"
        return True, "执行成功"

def install_comp():
    global initialized
    logger.info(f"{os.getpid()}:Start installing necessary components of SomeUtils/Coders")
    logger.info(f"{os.getpid()}:Start enabling WSL...")
    os.system("dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart")
    logger.info(f"{os.getpid()}:Start enabling VMP...")
    os.system("dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart")
    logger.info(f"{os.getpid()}:Start installing WSL...")
    os.system("wsl --set-default-version 2")
    os.system("wsl --install")
    os.system("wsl --upgrade")
    logger.info(f"{os.getpid()}:Start downloading docker...")
    url = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
    response = requests.get(url, stream=True)
    with open("DockerInstaller.exe", "wb") as file:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                file.write(chunk)
    logger.info(f"{os.getpid()}:Start installing docker...")
    os.system("./DockerInstaller.exe install --accept-license --backend=wsl-2 --always-run-service --quiet")
    logger.info(f"{os.getpid()}:Installation complete.Start pulling image...")
    client_global = docker.from_env()
    client_global.images.pull("python", tag="3.14.0rc1-bookworm")
    logger.info(f"{os.getpid()}:Image pulling complete.Components installation finishes.")
    with open(".installed", 'w') as file:
        file.write('COMP INSTALLED SIGN FILE')
    initialized = True

@register_plugin # 注册插件
class BotUtilsPlugin(BasePlugin):
    """
        插件注册中心：
        - 聚合所有功能组件
        - 提供启用/禁用开关
        - 管理依赖关系
        """
    # 以下是插件基本信息和方法（必须填写）
    plugin_name = "QQ_Bot_Additional_Utils_Plugin"
    enable_plugin = True  # 启用插件
    dependencies = []  # 插件依赖列表（目前为空）
    python_dependencies = []  # Python依赖列表（目前为空）
    config_file_name = "config.toml"  # 配置文件名
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="0.1.0", description="配置文件版本"),
        },
        "plugin_settings":{
            "api_key": ConfigField(type=str,default='Your siliconflow api key',description='你的硅基流动API KEY'),
            "enable_code_tool": ConfigField(type=bool, default=True, description="是否启用代码工具集"),
            "enable_baidu_tool": ConfigField(type=bool, default=True, description="是否启用百度搜索工具集"),
            "enable_poke_action": ConfigField(type=bool, default=True, description="是否启用戳一戳功能"),
            "enable_mute_action": ConfigField(type=bool, default=True, description="是否启用禁言功能"),
            "enable_sign_action": ConfigField(type=bool, default=True, description="是否启用群签到功能"),
            "api_address": ConfigField(type=str, default='127.0.0.1:3000', description="NapNeko/NapCat的API地址"),
            "baidu_api_key": ConfigField(type=str, default='None', description="百度搜索的API KEY"),
        },
    }


    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]: # 获取插件组件
        """返回插件包含的组件列表（目前是空的）"""
        global api_key,enable_code_tool,enable_baidu_tool,enable_poke_action,api_address,baidu_api_key,enable_sign_action,initialized
        if self.get_config("plugin.enabled", True):
            api_key = self.get_config("plugin_settings.api_key", 'None')
            enable_poke_action = self.get_config("plugin_settings.enable_poke_action",True)
            enable_baidu_tool = self.get_config("plugin_settings.enable_baidu_tool",True)
            enable_code_tool = self.get_config("plugin_settings.get_code_tool",True)
            enable_sign_action = self.get_config("plugin_settings.get_sign_action", True)
            baidu_api_key = self.get_config("plugin_settings.baidu_api_key", True)
            api_address = self.get_config("plugin_settings.api_address","127.0.0.1:3000")
            if not os.path.exists('config.toml'):
                logger.error(f"{os.getpid()}:检测到您为第一次使用SomeUtils插件，配置文件未生成，请配置完配置文件后重启MaiMBot")
                logger.error(f"{os.getpid()}:正在退出...")
                raise KeyboardInterrupt
            else:
                if os.path.exists(".installed"):
                    initialized = True
                if not os.path.exists(".installed") and enable_code_tool:
                    inp = input("是否要安装DOCKER及其需要的WSL组件(Y/n):")
                    if inp == "Y":
                        proc = multiprocessing.Process(target=install_comp,args=())
                        proc.start()
                    else:
                        logger.error("请禁用SomeUtils/CODERS组件!")
                        time.sleep(5)
                        raise KeyboardInterrupt
            return [
                (BaiduSearchTool.get_tool_info(),BaiduSearchTool),
                (PokeAction.get_action_info(),PokeAction),
                (CodeGenTool.get_tool_info(),CodeGenTool),
                (CodeRunTool.get_tool_info(),CodeRunTool),
                (CodeSearchTool.get_tool_info(),CodeSearchTool),
                (MuteAction.get_action_info(),MuteAction),
                (SignAction.get_action_info(),SignAction),]
        else:

            return []


