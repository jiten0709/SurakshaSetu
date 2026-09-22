FROM eclipse-temurin:21.0.12_8-jdk-noble AS build
WORKDIR /src
COPY mvnw pom.xml ./
COPY .mvn .mvn
COPY src src
RUN --mount=type=cache,target=/root/.m2 ./mvnw -B -q package -DskipTests

FROM eclipse-temurin:21.0.12_8-jre-noble
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /src/target/domain-services.jar /app/app.jar
USER app
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
