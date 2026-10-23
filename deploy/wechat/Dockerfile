# Linux 版微信 + 发消息要用的 xdotool / xclip（ricwang/docker-wechat 基础上加几个包）
# 基础镜像删掉了系统用户，dpkg 的 statoverride 会报错：先清掉它再 apt
FROM ricwang/docker-wechat:latest
RUN : > /var/lib/dpkg/statoverride \
 && apt-get update && apt-get install -y --no-install-recommends xdotool xclip fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*
