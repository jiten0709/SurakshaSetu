package com.surakshasetu.domain;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noFields;
import static org.assertj.core.api.Assertions.assertThat;

import com.tngtech.archunit.core.importer.ClassFileImporter;
import com.tngtech.archunit.core.importer.ImportOption;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/**
 * Commission, margin and distributor incentives are never decision inputs (TDD §3.8; the contract
 * bans them too). Fails the build if a decision class grows such a field, or the rules or
 * parameters name one. Step 7 extends it to the ranking weights.
 */
class CommercialFieldBanTest {

  private static final String BANNED = "commission|margin|incentive|payout|campaign|brokerage";

  @Test
  void noDecisionClassHasACommercialField() {
    noFields()
        .that()
        .areDeclaredInClassesThat()
        .resideInAnyPackage(
            "com.surakshasetu.domain.eligibility..",
            "com.surakshasetu.domain.suitability..",
            "com.surakshasetu.domain.ranking..",
            "com.surakshasetu.domain.quote..")
        .should()
        .haveNameMatching("(?i).*(" + BANNED + ").*")
        .allowEmptyShould(true)
        .check(
            new ClassFileImporter()
                .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_TESTS)
                .importPackages("com.surakshasetu.domain"));
  }

  @Test
  void noRuleOrParameterNamesOne() throws Exception {
    Pattern banned = Pattern.compile(BANNED, Pattern.CASE_INSENSITIVE);
    List<Path> files;
    try (Stream<Path> walk = Files.walk(Path.of("src", "main", "resources"))) {
      files = walk.filter(p -> p.toString().matches(".*\\.(dmn|yaml)$")).toList();
    }
    assertThat(files).isNotEmpty();
    for (Path file : files) {
      assertThat(banned.matcher(Files.readString(file)).find()).as(file.toString()).isFalse();
    }
  }
}
