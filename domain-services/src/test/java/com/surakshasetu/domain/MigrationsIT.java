package com.surakshasetu.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.List;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.postgresql.PostgreSQLContainer;

/** Applies infra/db/migrations as `make db-migrate` does, so CI proves them on every push. */
@Testcontainers
class MigrationsIT {

  // Same image as infra/compose.yaml; its own server, so both databases start empty.
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

    // V8 (OD-1): a disclosure is keyed by (disclosure_id, language).
    List<String> key = new ArrayList<>();
    try (var conn =
            DriverManager.getConnection(
                POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword());
        var rows =
            conn.createStatement()
                .executeQuery(
                    """
                    SELECT a.attname FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
                    WHERE i.indrelid = 'catalog.disclosure'::regclass AND i.indisprimary
                    ORDER BY array_position(i.indkey, a.attnum)
                    """)) {
      while (rows.next()) {
        key.add(rows.getString(1));
      }
    }
    assertThat(key).containsExactly("disclosure_id", "language");
  }

  private static Flyway flyway(String database) {
    String url =
        "jdbc:postgresql://%s:%d/%s"
            .formatted(POSTGRES.getHost(), POSTGRES.getMappedPort(5432), database);
    return DomainApiTestSupport.flyway(url, POSTGRES.getUsername(), POSTGRES.getPassword());
  }
}
