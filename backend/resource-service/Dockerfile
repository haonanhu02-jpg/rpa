# syntax=docker/dockerfile:1

# 多阶段构建 Dockerfile for Java 21 Spring Boot Application

# 第一阶段：构建JAR包
FROM --platform=$BUILDPLATFORM maven:3.9-eclipse-temurin-21 AS build

# 设置工作目录
WORKDIR /app
ARG MAVEN_MIRROR_URL=https://maven.aliyun.com/repository/public
ENV MAVEN_MIRROR_URL=${MAVEN_MIRROR_URL}
COPY docker/maven/ /opt/astron-rpa-maven/

# 复制Maven配置文件
COPY backend/resource-service/pom.xml .

# 下载依赖（利用Docker缓存层）
RUN sh /opt/astron-rpa-maven/mvn-rpa.sh dependency:go-offline

# 复制源代码
COPY backend/resource-service/src ./src

# 构建应用
RUN sh /opt/astron-rpa-maven/mvn-rpa.sh clean package -DskipTests

# 第二阶段：构建可执行镜像
FROM eclipse-temurin:21-jre-noble AS runtime

# 设置工作目录
WORKDIR /app

# 从构建阶段复制JAR文件
COPY --from=build /app/target/*.jar app.jar

# 复制配置文件（如果存在）
# COPY docker/.env ./.env

# 暴露端口
EXPOSE 8030

# 设置JVM参数
ENV JAVA_OPTS="-Xms512m -Xmx1024m -XX:+UseG1GC -XX:+UseContainerSupport"

# 启动应用
ENTRYPOINT ["sh", "-c", "java $JAVA_OPTS -jar app.jar"]
