package com.surakshasetu.domain.quote;

import static org.assertj.core.api.Assertions.assertThat;

import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.contract.model.Gender;
import com.surakshasetu.domain.contract.model.PptOption;
import com.surakshasetu.domain.quote.RatingEngine.Basis;
import com.surakshasetu.domain.quote.RatingEngine.Premium;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.core.io.FileSystemResource;

/** Golden premiums from the DUMMY table, worked by hand (GST 0.00 unless stated). */
class StubRatingEngineTest {

  private static final Path TABLE =
      Path.of("src", "main", "resources", "rating", "dummy-rates-2026.09.1.yaml");
  private static final String TERM = "999N001V02";
  private static final String ROP = "999N002V01";

  private final StubRatingEngine engine = engine(TABLE);

  @Test
  void theTableIsDummyAndUngendered() {
    assertThat(engine.version()).isEqualTo("rating-dummy-2026.09.1");
    assertThat(engine.ratesByGender(TERM)).isFalse();
    assertThat(engine.ratesByGender(ROP)).isFalse();
  }

  @Test
  void termShieldGoldenPremiums() {
    // 34, non-tobacco, ₹1 crore, 30 years, regular: band 31–40 × 21–30 = 1.60.
    // 1,00,00,000 / 1,000 × 1.60 = 16,000.
    assertThat(total(TERM, 34, false, "10000000", 30, PptOption.REGULAR)).isEqualTo("16000.00");
    // 45, tobacco, ₹50 lakh, 20 years, limited_10: band 41–50 × 10–20 = 2.60.
    // 5,000 × 2.60 × 1.70 (limited_10) × 1.80 (tobacco) = 5,000 × 7.956 = 39,780.
    assertThat(total(TERM, 45, true, "5000000", 20, PptOption.LIMITED_10)).isEqualTo("39780.00");
    // 25, non-tobacco, ₹2 crore, 35 years, single, accidental death rider: band 18–30 × 31–40.
    // Base 20,000 × 1.10 × 9 (single) = 1,98,000; rider 20,000 × 0.30 × 9 = 54,000.
    Premium p =
        engine
            .rate(basis(TERM, 25, false, null, "20000000", 35, PptOption.SINGLE, "999A007V01"))
            .orElseThrow();
    assertThat(p.riders()).containsExactly(Map.entry("999A007V01", new BigDecimal("54000.00")));
    assertThat(p.total()).isEqualByComparingTo("252000");
  }

  @Test
  void termShieldRopGoldenPremiums() {
    // 30, non-tobacco, ₹25 lakh, 20 years, regular: band 18–30 × 15–25 = 2.80.
    // 2,500 × 2.80 = 7,000.
    assertThat(total(ROP, 30, false, "2500000", 20, PptOption.REGULAR)).isEqualTo("7000.00");
    // 40, tobacco, ₹1 crore, 30 years, limited_10: band 31–40 × 26–35 = 4.20.
    // 10,000 × 4.20 × 1.55 × 1.60 = 10,000 × 10.416 = 1,04,160.
    assertThat(total(ROP, 40, true, "10000000", 30, PptOption.LIMITED_10)).isEqualTo("104160.00");
    // 50, non-tobacco, ₹50 lakh, 25 years, regular, accidental death rider: band 41–55 × 15–25.
    // Base 5,000 × 6.50 = 32,500; rider 5,000 × 0.30 = 1,500; total 34,000.
    Premium p =
        engine
            .rate(basis(ROP, 50, false, null, "5000000", 25, PptOption.REGULAR, "999A007V01"))
            .orElseThrow();
    assertThat(p.riders().get("999A007V01")).isEqualByComparingTo("1500");
    assertThat(p.total()).isEqualByComparingTo("34000");
  }

  @Test
  void bandEdgesAreInclusive() {
    // 30 × 20 is the first cell (0.80); 31 × 21 is the fifth (1.60). ₹10 lakh: 1,000 × rate.
    assertThat(total(TERM, 30, false, "1000000", 20, PptOption.REGULAR)).isEqualTo("800.00");
    assertThat(total(TERM, 31, false, "1000000", 21, PptOption.REGULAR)).isEqualTo("1600.00");
  }

  @Test
  void everyAgeAndTermTheSeedAllowsHasExactlyOneRate() {
    // The seed's entry ages, terms and maturity ages (content/seed/catalog/products.yaml).
    assertCovered(TERM, 18, 65, 10, 40, 85);
    assertCovered(ROP, 18, 55, 15, 35, 75);
  }

  @Test
  void whatTheTableDoesNotCoverCannotBeRated() {
    assertThat(
            engine.rate(basis("999N010V01", 30, false, null, "1000000", 15, PptOption.LIMITED_10)))
        .as("savings has no rates")
        .isEmpty();
    assertThat(engine.rate(basis(TERM, 66, false, null, "10000000", 10, PptOption.REGULAR)))
        .isEmpty();
    assertThat(engine.rate(basis(ROP, 30, false, null, "10000000", 20, PptOption.SINGLE)))
        .as("ROP has no single-pay loading")
        .isEmpty();
    assertThat(
            engine.rate(
                basis(TERM, 30, false, null, "10000000", 20, PptOption.REGULAR, "999A099V01")))
        .isEmpty();
  }

  @Test
  void gstIsAppliedFromTheTable(@TempDir Path dir) throws Exception {
    Path table = dir.resolve("rates.yaml");
    Files.writeString(
        table, Files.readString(TABLE).replace("gst_rate: \"0.00\"", "gst_rate: \"0.18\""));
    StubRatingEngine taxed = engine(table);
    // 16,000 × 1.18 = 18,880.
    assertThat(taxed.rate(basis(TERM, 34, false, null, "10000000", 30, PptOption.REGULAR)))
        .map(p -> p.total().toPlainString())
        .contains("18880.00");
  }

  @Test
  void aGenderRatedProductNeedsTheGender(@TempDir Path dir) throws Exception {
    Path table = dir.resolve("rates.yaml");
    Files.writeString(
        table,
        Files.readString(TABLE)
            .replaceFirst(
                "gender_loading: \\{}",
                "gender_loading: {male: \"1.00\", female: \"0.85\", transgender: \"1.00\"}"));
    StubRatingEngine rated = engine(table);
    assertThat(rated.ratesByGender(TERM)).isTrue();
    assertThat(rated.ratesByGender(ROP)).isFalse();
    // 16,000 × 0.85 = 13,600.
    assertThat(rated.rate(basis(TERM, 34, false, Gender.FEMALE, "10000000", 30, PptOption.REGULAR)))
        .map(p -> p.total().toPlainString())
        .contains("13600.00");
    assertThat(rated.rate(basis(TERM, 34, false, null, "10000000", 30, PptOption.REGULAR)))
        .isEmpty();
  }

  private void assertCovered(
      String uin, int ageMin, int ageMax, int termMin, int termMax, int maturityMax) {
    StubRatingEngine.ProductRates rates = table().products().get(uin);
    for (int age = ageMin; age <= ageMax; age++) {
      for (int term = termMin; term <= termMax && age + term <= maturityMax; term++) {
        int a = age;
        int t = term;
        assertThat(
                rates.baseRates().stream()
                    .filter(c -> c.ageMin() <= a && a <= c.ageMax())
                    .filter(c -> c.termMin() <= t && t <= c.termMax())
                    .count())
            .as("%s at age %d, term %d", uin, age, term)
            .isOne();
      }
    }
  }

  private StubRatingEngine.Table table() {
    try {
      return Rules.YAML.readValue(Files.readAllBytes(TABLE), StubRatingEngine.Table.class);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }

  private String total(String uin, int age, boolean tobacco, String sa, int term, PptOption ppt) {
    Optional<Premium> premium = engine.rate(basis(uin, age, tobacco, null, sa, term, ppt));
    assertThat(premium).isPresent();
    assertThat(premium.orElseThrow().riders()).isEmpty();
    return premium.orElseThrow().total().toPlainString();
  }

  private static Basis basis(
      String uin,
      int age,
      boolean tobacco,
      @Nullable Gender gender,
      String sa,
      int term,
      PptOption ppt,
      String... riders) {
    return new Basis(uin, age, tobacco, gender, new BigDecimal(sa), term, ppt, List.of(riders));
  }

  private static StubRatingEngine engine(Path table) {
    try {
      return new StubRatingEngine(new FileSystemResource(table));
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
