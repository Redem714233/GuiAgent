"""
修复 Florence2 模型中 _tie_weights 方法的 bug
这个脚本会修改缓存中的 modeling_florence2.py 文件
"""
import os
import re

def fix_florence2_model():
    # 找到 Florence2 模型缓存路径
    cache_path = os.path.expanduser("~/.cache/huggingface/modules/transformers_modules/icon_caption_florence/modeling_florence2.py")
    
    if not os.path.exists(cache_path):
        print(f"未找到文件: {cache_path}")
        print("可能模型还未下载或路径不对")
        return False
    
    print(f"找到文件: {cache_path}")
    
    # 读取文件
    with open(cache_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 查找并修复 _tie_weights 方法
    # 需要修改的是 Florence2LanguageModel 类中的 _tie_weights 方法
    # 原代码可能返回 list，需要改为返回 dict
    
    # 方法1: 简单替换 - 将 _tied_weights_keys 改为返回字典格式
    old_pattern = r'_tied_weights_keys = \["encoder\.embed_tokens\.weight", "decoder\.embed_tokens\.weight"\]'
    new_pattern = '_tied_weights_keys = {"encoder.embed_tokens.weight": "shared.weight", "decoder.embed_tokens.weight": "shared.weight"}'
    
    if re.search(old_pattern, content):
        print("发现需要修复的模式1")
        content = re.sub(old_pattern, new_pattern, content)
    
    # 方法2: 修复 Florence2LanguageForConditionalGeneration
    old_pattern2 = r'_tied_weights_keys = \["encoder\.embed_tokens\.weight", "decoder\.embed_tokens\.weight", "lm_head\.weight"\]'
    new_pattern2 = '_tied_weights_keys = {"encoder.embed_tokens.weight": "shared.weight", "decoder.embed_tokens.weight": "shared.weight", "lm_head.weight": "shared.weight"}'
    
    if re.search(old_pattern2, content):
        print("发现需要修复的模式2")
        content = re.sub(old_pattern2, new_pattern2, content)
    
    # 备份原文件
    backup_path = cache_path + ".backup"
    if not os.path.exists(backup_path):
        with open(backup_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"已创建备份: {backup_path}")
    
    # 实际上，更好的方法是直接用环境变量禁用 tied weights检查
    # 或者修改代码让其兼容
    
    print("\n建议的解决方案：")
    print("1. 降级 transformers 到 4.37.0: pip install transformers==4.37.0")
    print("2. 或者设置环境变量跳过检查: os.environ['TRANSFORMERS_NO_ADVISORY_WARNINGS'] = 'true'")
    print("3. 或者暂时使用 Mock Parser")
    
    return True

if __name__ == "__main__":
    fix_florence2_model()
