package com.surakshasetu.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.postgresql.PostgreSQLContainer;

/** Applies infra/db/migrations as `make db-migrate` does, so CI proves them on every push. */
@Testcontainers
class MigrationsIT {

  // Same image as infra/compose.yaml.
  @Container
  static final PostgreSQLContainer POSTGRES =
      new PostgreSQLContainer("postgres:16.15-trixie").withDatabaseName("surakshasetu");

  @Test
  void migratesBothDatabasesIdempotentlyAndValidates() throws Exception {
    // Roles are cluster-wide, so the second database proves V1 tolerates existing roles.
    try (var conn =
            DriverManager.getConnection(
                POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword());
        var statement = conn.createStatement()) {
      statement.execute("CREATE DATABASE surakshasetu_test");
    }

    for (String database : List.of("surakshasetu", "surakshasetu_test")) {
      Flyway flyway = flyway(database);
      assertThat(flyway.migrate().migrationsExecuted).isPositive();
      assertThat(flyway.migrate().migrationsExecuted).isZero();
      flyway.validate();
    }
  }

  private static Flyway flyway(String database) {
    String url =
        "jdbc:postgresql://%s:%d/%s"
            .formatted(POSTGRES.getHost(), POSTGRES.getMappedPort(5432), database);
    return Flyway.configure()
        .dataSource(url, POSTGRES.getUsername(), POSTGRES.getPassword())
        .locations("filesystem:../infra/db/migrations")
        .failOnMissingLocations(true)
        .defaultSchema("flyway")
        .placeholders(
            Stream.of(
                    "app_rw",
                    "domain_rw",
                    "catalog_loader",
                    "erasure_rw",
                    "keyvault_rw",
                    "compliance_ro")
                .collect(Collectors.toMap(role -> role + "_password", role -> "it-only")))
        .load();
  }
}
