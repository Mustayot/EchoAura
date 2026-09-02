把你的 Live2D 模型文件夹放进这里（里面有 .model3.json 的那个文件夹），例如：

    models\我的角色\
        ├── 我的角色.model3.json
        ├── 我的角色.moc3
        ├── 我的角色.physics3.json
        ├── 我的角色.model3.json
        ├── textures\  （贴图）
        └── motions\   （动画）

一个文件夹 = 一个模型，可以放多个。
然后打开 config.py 设置：
    MODEL_PATH = r"models"        # 模型根目录（一般不用改）
    MODEL_NAME = "我的角色"        # 要用哪个（文件夹名），留空=自动用第一个
