package com.surakshasetu.domain;

import static com.atlassian.oai.validator.mockmvc.OpenApiValidationMatchers.openApi;

import com.atlassian.oai.validator.OpenApiInteractionValidator;
import com.atlassian.oai.validator.report.LevelResolver;
import com.atlassian.oai.validator.report.ValidationReport;
import com.surakshasetu.domain.catalog.SeedLoader;
import java.nio.file.Path;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.context.WebApplicationContext;
import org.testcontainers.postgresql.PostgreSQLContainer;

/**
 * One Postgres per test JVM, migrated from infra/db/migrations and seeded from content/seed. The
 * application connects as domain_rw (and catalog_loader), exactly as in compose; {@link #ADMIN} is
 * the superuser, for fixtures only. Every {@link #api} response is checked against the tier
 * contract. Tests that mutate data use their own keys (language xx-TEST, channel tamper-test,
 * product 999N099V01), so classes share one context and one database.
 */
@SpringBootTest(properties = "logging.level.com.surakshasetu=DEBUG")
public abstract class DomainApiTestSupport {

  public static final Path SEED = Path.of("..", "content", "seed");
  public static final String ROLE_PASSWORD = "it-only";

  // Same image as infra/compose.yaml. Started once; Testcontainers removes it when the JVM exits.
  private static final PostgreSQLContainer POSTGRES =
      new PostgreSQLContainer("postgres:16.15-trixie").withDatabaseName("surakshasetu");
  protected static final JdbcClient ADMIN;
  private static final String SPEC =
      Path.of("..", "contracts", "openapi", "domain-services.v1.yaml")
          .toAbsolutePath()
          .toUri()
          .toString();
  protected static final OpenApiInteractionValidator CONTRACT =
      OpenApiInteractionValidator.createForSpecificationUrl(SPEC).build();
  // For requests that break the contract on purpose: only the response is checked.
  private static final OpenApiInteractionValidator RESPONSES =
      OpenApiInteractionValidator.createForSpecificationUrl(SPEC)
          .withLevelResolver(
              LevelResolver.create()
                  .withLevel("validation.request", ValidationReport.Level.IGNORE)
                  .build())
          .build();
  private static boolean seeded;

  static {
    POSTGRES.start();
    flyway(POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword()).migrate();
    ADMIN =
        JdbcClient.create(
            new DriverManagerDataSource(
                POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword()));
  }

  @Autowired protected WebApplicationContext context;
  @Autowired protected SeedLoader seedLoader;
  protected MockMvc mvc;

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", POSTGRES::getJdbcUrl);
    registry.add("spring.datasource.username", () -> "domain_rw");
    registry.add("spring.datasource.password", () -> ROLE_PASSWORD);
    registry.add("surakshasetu.catalog-loader.password", () -> ROLE_PASSWORD);
  }

  /**
   * Flyway as `make db-migrate` runs it, with every role password set to {@link #ROLE_PASSWORD}.
   */
  public static Flyway flyway(String url, String user, String password) {
    return Flyway.configure()
        .dataSource(url, user, password)
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
                .collect(Collectors.toMap(role -> role + "_password", role -> ROLE_PASSWORD)))
        .load();
  }

  @BeforeEach
  void setUpMockMvc() {
    mvc = MockMvcBuilders.webAppContextSetup(context).build();
    synchronized (DomainApiTestSupport.class) {
      if (!seeded) {
        seedLoader.load(SEED);
        seeded = true;
      }
    }
  }

  /** Performs the request as the orchestrator would, and fails unless it matches the contract. */
  protected ResultActions api(MockHttpServletRequestBuilder request) throws Exception {
    return mvc.perform(request.header("Authorization", "Bearer test-token"))
        .andExpect(openApi().isValid(CONTRACT));
  }

  /** A request that breaks the contract on purpose; the response must still match it. */
  protected ResultActions apiRejecting(MockHttpServletRequestBuilder request) throws Exception {
    return mvc.perform(request.header("Authorization", "Bearer test-token"))
        .andExpect(openApi().isValid(RESPONSES));
  }
}
