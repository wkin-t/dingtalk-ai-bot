import inspect
import pkgutil
import importlib

# 目标模块列表
target_modules = [
    "alibabacloud_dingtalk.im_1_0.models",
    "alibabacloud_dingtalk.robot_1_0.models",
    "alibabacloud_dingtalk.oauth2_1_0.models",
    "alibabacloud_dingtalk.contact_1_0.models"
]

print("🔍 Searching for 'chat' in ResponseBody models...")

for module_name in target_modules:
    try:
        module = importlib.import_module(module_name)
        print(f"\n📦 Scanning module: {module_name}")
        
        for name, cls in inspect.getmembers(module, inspect.isclass):
            # 只关心 ResponseBody
            if "ResponseBody" in name:
                try:
                    # 检查 __init__ 参数
                    init_sig = inspect.signature(cls.__init__)
                    found = False
                    for param_name in init_sig.parameters:
                        if "chat" in param_name.lower():
                            print(f"  ✨ Found in {name}: {param_name}")
                            found = True
                    
                    # 如果 __init__ 没找到，检查 _map (Tea Model 特性)
                    if not found and hasattr(cls, '_map'):
                        for key in cls._map.keys():
                            if "chat" in key.lower():
                                print(f"  ✨ Found in {name} (_map): {key}")
                except:
                    pass
    except ImportError:
        print(f"⚠️ Module not found: {module_name}")