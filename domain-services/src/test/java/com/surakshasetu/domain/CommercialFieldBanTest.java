package com.surakshasetu.domain;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noFields;
import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.common.Rules;
import com.tngtech.archunit.core.importer.ClassFileImporter;
import com.tngtech.archunit.core.importer.ImportOption;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;

/**
 * Commission, margin and distributor incentives are never decision inputs (TDD §3.8; the contract
 * bans them too). Fails the build if a decision class or a generated contract model grows such a
 * field, or if a rules, parameter, ranking-weights or rate file names one.
 */
class CommercialFieldBanTest {

  private static final String BANNED = "commission|margin|incentive|payout|campaign|brokerage";
  private static final Path RESOURCES = Path.of("src", "main", "resources");
  private static final Path WEIGHTS = RESOURCES.resolve("ranking/weights-2026.09.1.yaml");

  @Test
  void noDecisionClassHasACommercialField() {
    noFields()
        .that()
        .areDeclaredInClassesThat()
        .resideInAnyPackage(
            "com.surakshasetu.domain.eligibility..",
            "com.surakshasetu.domain.suitability..",
            "com.surakshasetu.domain.ranking..",
            "com.surakshasetu.domain.quote..",
            "com.surakshasetu.domain.contract..")
        .should()
        .haveNameMatching("(?i).*(" + BANNED + ").*")
        .allowEmptyShould(true)
        .check(
            new ClassFileImporter()
                .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_TESTS)
                .importPackages("com.surakshasetu.domain"));
  }

  @Test
  void noRuleParameterWeightOrRateFileNamesOne() throws Exception {
    Pattern banned = Pattern.compile(BANNED, Pattern.CASE_INSENSITIVE);
    List<Path> files;
    try (Stream<Path> walk = Files.walk(RESOURCES)) {
      files = walk.filter(p -> p.toString().matches(".*\\.(dmn|yaml)$")).toList();
    }
    assertThat(files)
        .contains(WEIGHTS, RESOURCES.resolve("rating/dummy-rates-2026.09.1.yaml"))
        .anyMatch(p -> p.toString().endsWith(".dmn"));
    for (Path file : files) {
      assertThat(banned.matcher(Files.readString(file)).find()).as(file.toString()).isFalse();
    }
  }

  @Test
  void noRankingWeightKeyIsOne() throws Exception {
    List<String> keys = new ArrayList<>();
    collectKeys(Rules.YAML.readTree(Files.readAllBytes(WEIGHTS)), keys);
    assertThat(keys).contains("weights", "need_coverage", "rider_rules", "reason_code");
    assertThat(keys).noneMatch(k -> k.matches("(?i).*(" + BANNED + ").*"));
  }

  private static void collectKeys(JsonNode node, List<String> keys) {
    if (node.isObject()) {
      keys.addAll(node.propertyNames());
    }
    node.values().forEach(child -> collectKeys(child, keys));
  }
}
