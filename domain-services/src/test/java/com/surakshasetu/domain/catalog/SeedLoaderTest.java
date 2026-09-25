package com.surakshasetu.domain.catalog;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.surakshasetu.domain.DomainApiTestSupport;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class SeedLoaderTest extends DomainApiTestSupport {

  @Test
  void aReRunOfUnchangedContentChangesNothing() {
    assertThat(seedLoader.load(SEED)).isZero();
  }

  @Test
  void theSeedMatchesTdd73AndIsFlaggedDummy() {
    assertThat(count("SELECT count(*) FROM catalog.product WHERE uin <> '999N099V01'"))
        .isEqualTo(3);
    assertThat(count("SELECT count(*) FROM catalog.rider")).isEqualTo(3);
    assertThat(count("SELECT count(*) FROM catalog.product_document")).isEqualTo(6);
    assertThat(
            count(
                "SELECT count(*) FROM catalog.disclosure WHERE disclosure_id LIKE 'DISC-%'"
                    + " AND body LIKE 'DUMMY:%'"))
        .isEqualTo(9 * 2);
    assertThat(
            count(
                "SELECT count(*) FROM catalog.disclosure_set"
                    + " WHERE channel IN ('web', 'app') AND language IN ('en-IN', 'hi-IN')"))
        .isEqualTo(2 * 2 * 2);
    assertThat(count("SELECT count(*) FROM catalog.pincode")).isEqualTo(20);
    assertThat(count("SELECT count(*) FROM catalog.occupation")).isEqualTo(10);
    assertThat(count("SELECT count(DISTINCT risk_class) FROM catalog.occupation")).isEqualTo(4);
    assertThat(count("SELECT count(*) FROM consent.notice_version WHERE body LIKE 'DUMMY:%'"))
        .isEqualTo(2);
    for (String table :
        new String[] {
          "catalog.product",
          "catalog.rider",
          "catalog.product_document",
          "catalog.disclosure",
          "catalog.pincode",
          "catalog.occupation",
          "consent.notice_version"
        }) {
      assertThat(count("SELECT count(*) FROM " + table + " WHERE NOT is_dummy")).isZero();
    }
  }

  @Test
  void aChangedBodyUnderTheSameVersionFailsTheLoad(@TempDir Path copy) throws IOException {
    copySeed(copy);
    edit(
        copy.resolve("catalog/disclosures.yaml"),
        "en-IN: \"DUMMY: 30-day free-look period.\"",
        "en-IN: \"DUMMY: 15-day free-look period.\"");

    assertThatThrownBy(() -> seedLoader.load(copy))
        .isInstanceOf(SeedLoader.SeedConflict.class)
        .hasMessageContaining("DISC-GLOBAL-FREELOOK-04")
        .hasMessageContaining("new registry_version");
    assertThat(
            ADMIN
                .sql(
                    "SELECT body FROM catalog.disclosure"
                        + " WHERE disclosure_id = 'DISC-GLOBAL-FREELOOK-04' AND language = 'en-IN'")
                .query(String.class)
                .single())
        .isEqualTo("DUMMY: 30-day free-look period.");
  }

  @Test
  void aReorderedSetUnderTheSameVersionFailsTheLoad(@TempDir Path copy) throws IOException {
    copySeed(copy);
    edit(
        copy.resolve("catalog/disclosures.yaml"),
        "      - DISC-GLOBAL-QUOTE-02\n      - DISC-UIN-999N001V02-21\n",
        "      - DISC-UIN-999N001V02-21\n      - DISC-GLOBAL-QUOTE-02\n");

    assertThatThrownBy(() -> seedLoader.load(copy))
        .isInstanceOf(SeedLoader.SeedConflict.class)
        .hasMessageContaining("new registry_version is required");
  }

  @Test
  void aChangedNoticeOrDocumentFailsTheLoad(@TempDir Path copy) throws IOException {
    copySeed(copy);
    edit(copy.resolve("consent/notices.yaml"), "[P3 optional]", "[P3 optional, changed]");
    assertThatThrownBy(() -> seedLoader.load(copy))
        .isInstanceOf(SeedLoader.SeedConflict.class)
        .hasMessageContaining("new notice_version");

    copySeed(copy);
    Files.writeString(copy.resolve("kb/product/999N001V02-cis.md"), "DUMMY: rewritten\n");
    assertThatThrownBy(() -> seedLoader.load(copy))
        .isInstanceOf(SeedLoader.SeedConflict.class)
        .hasMessageContaining("new document version");
  }

  private static long count(String sql) {
    return ADMIN.sql(sql).query(Long.class).single();
  }

  /** content/seed into {@code target}, overwriting what is there. */
  private static void copySeed(Path target) throws IOException {
    try (Stream<Path> files = Files.walk(SEED)) {
      for (Path source : files.filter(Files::isRegularFile).toList()) {
        Path dest = target.resolve(SEED.relativize(source).toString());
        Files.createDirectories(dest.getParent());
        Files.copy(source, dest, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
      }
    }
  }

  private static void edit(Path file, String from, String to) throws IOException {
    String text = Files.readString(file);
    assertThat(text).contains(from);
    Files.writeString(file, text.replace(from, to));
  }
}
