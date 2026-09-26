package com.surakshasetu.domain.ranking;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.surakshasetu.domain.catalog.CatalogRepository.QuoteDefaults;
import com.surakshasetu.domain.contract.model.Frequency;
import com.surakshasetu.domain.contract.model.Pins;
import com.surakshasetu.domain.contract.model.PptOption;
import com.surakshasetu.domain.contract.model.PremiumQuote;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.ProductType;
import com.surakshasetu.domain.contract.model.QuoteRequest;
import com.surakshasetu.domain.contract.model.RankingRequest;
import com.surakshasetu.domain.contract.model.RecommendedOption;
import com.surakshasetu.domain.contract.model.Rider;
import com.surakshasetu.domain.contract.model.SuitabilityAssumptions;
import com.surakshasetu.domain.contract.model.SuitabilityResult;
import com.surakshasetu.domain.contract.model.SuitabilityResult.AffordabilityEnum;
import com.surakshasetu.domain.contract.model.SuitabilityResult.OutcomeEnum;
import com.surakshasetu.domain.quote.QuoteAdapter;
import com.surakshasetu.domain.ranking.Ranker.Candidate;
import com.surakshasetu.domain.ranking.Ranker.Ranked;
import com.surakshasetu.domain.ranking.Ranker.ScoreWeights;
import com.surakshasetu.domain.ranking.Ranker.Weights;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The ranker as a pure function over synthetic products, with a fake quote adapter: premiums by
 * UIN, empty for a UIN it can't rate. Weights are the shipped DUMMY file (0.60 / 0.30 / 0.10).
 */
class RankerTest {

  private static final Path WEIGHTS = Path.of("src", "main", "resources", "ranking");
  private static final String RULES = "rules-2026.09.1";
  private static final BigDecimal STEP = new BigDecimal("500000");
  private static final String ADB = "999A007V01";
  private static final String WOP = "999A008V01";
  private static final String CI = "999A009V01";

  private static final Weights SHIPPED = load();

  /** Premiums the fake adapter quotes, by UIN; a missing UIN can't be rated. */
  private final Map<String, String> premiums = new HashMap<>();

  /** Every request the ranker priced. */
  private final List<QuoteRequest> priced = new ArrayList<>();

  private final Map<String, Product> products = new HashMap<>();

  @BeforeEach
  void catalog() {
    // Entry 18–65, maturity 85, cover ₹25 lakh–₹10 crore, term 10–40, like the seed's 999N001V02.
    for (String uin : List.of("999N001V01", "999N002V01", "999N003V01", "999N004V01")) {
      products.put(uin, product(uin, ProductType.TERM, "100000000", ADB, WOP, CI));
    }
    products.put("999N005V01", product("999N005V01", ProductType.TERM_ROP, "20000000", ADB));
    products.put("999N010V01", product("999N010V01", ProductType.NON_PAR_SAVINGS, "10000000"));
  }

  @Test
  void candidatesAreEligibleFittingAndNotExcluded() {
    RankingRequest request =
        request("999N001V01", "999N002V01", "999N005V01", "999N010V01")
            .excludedUins(List.of("999N002V01"));
    // 999N003V01 and 999N004V01 aren't eligible; savings isn't a fit type; 002 was excluded.
    Ranked ranked = rank(request);
    assertThat(uins(ranked.options())).containsExactlyInAnyOrder("999N001V01", "999N005V01");
    assertThat(ranked.candidates()).isEqualTo(2);
  }

  @Test
  void savingsNeverRanksWhenSuitabilityExcludedIt() {
    RankingRequest request = request("999N010V01");
    Ranked ranked = rank(request);
    assertThat(ranked.options()).isEmpty();
    assertThat(ranked.candidates()).isZero();
  }

  @Test
  void theTddExampleRanksCoverFirst() {
    // 34, needs ₹3.75 crore for 26 years, green, 26 dependency years: all three riders fit.
    premiums.put("999N001V01", "106875"); // full cover, all three riders
    premiums.put("999N005V01", "90000"); // capped at ₹2 crore, accidental death only
    List<RecommendedOption> options = rank(request("999N001V01", "999N005V01")).options();
    // 001: 0.60 × 1 + 0.30 × (90,000 / 1,06,875 = 0.842105) + 0.10 × 3/3 = 0.952632.
    // 005: 0.60 × (2 / 3.75 = 0.533333) + 0.30 × 1 + 0.10 × 1/3 = 0.653333.
    assertThat(uins(options)).containsExactly("999N001V01", "999N005V01");
    RecommendedOption first = options.get(0);
    assertThat(first.getRank()).isEqualTo(1);
    assertThat(first.getSumAssuredInr()).isEqualTo("37500000");
    assertThat(first.getTermYears()).isEqualTo(26);
    assertThat(first.getPptYears()).isEqualTo(26);
    assertThat(first.getRiderUins()).containsExactly(ADB, WOP, CI);
    assertThat(first.getProtectionGapInr()).isEqualTo("0");
    assertThat(first.getReasonCodes())
        .containsExactly("RANK-FIT-TERM", "RANK-RIDER-ADB", "RANK-RIDER-WOP", "RANK-RIDER-CI");
    RecommendedOption second = options.get(1);
    assertThat(second.getSumAssuredInr()).isEqualTo("20000000");
    assertThat(second.getProtectionGapInr()).isEqualTo("17500000");
    assertThat(second.getRiderUins()).containsExactly(ADB);
    assertThat(second.getReasonCodes())
        .containsExactly("RANK-FIT-TERM_ROP", "RANK-SA-CAPPED", "RANK-RIDER-ADB");
  }

  @Test
  void affordabilityCanOutweighCover() {
    premiums.put("999N001V01", "300000");
    premiums.put("999N005V01", "100000");
    // Needs ₹2.25 crore: 001 covers it all, 005 is capped at ₹2 crore.
    // 001: 0.60 × 1 + 0.30 × (1,00,000 / 3,00,000) + 0.10 = 0.800000.
    // 005: 0.60 × (2 / 2.25 = 0.888889) + 0.30 × 1 + 0.10 × 1/3 = 0.866667.
    RankingRequest request = request("999N001V01", "999N005V01");
    request.getSuitability().setRecommendedCoverInr("22500000");
    assertThat(uins(rank(request).options())).containsExactly("999N005V01", "999N001V01");
  }

  @Test
  void tiesGoToTheLowerPremiumThenTheUin() {
    // With no weight on affordability, equal cover and riders score the same.
    Weights coverOnly =
        new Weights(
            "ranker-test",
            RULES,
            "compliance",
            true,
            new ScoreWeights(new BigDecimal("0.9"), BigDecimal.ZERO, new BigDecimal("0.1")),
            SHIPPED.riderRules());
    premiums.put("999N004V01", "50000");
    premiums.put("999N003V01", "40000");
    premiums.put("999N002V01", "40000");
    // 999N001V01 can't be rated: no premium, so last among equals.
    RankingRequest request = request("999N001V01", "999N002V01", "999N003V01", "999N004V01");
    Ranked ranked = Ranker.rank(request, candidates(), coverOnly, STEP, this::quote);
    assertThat(uins(ranked.options())).containsExactly("999N002V01", "999N003V01", "999N004V01");
    assertThat(ranked.candidates()).isEqualTo(4);
  }

  @Test
  void onlyTheTopThreeAreReturnedAndRanked() {
    premiums.put("999N001V01", "40000");
    premiums.put("999N002V01", "41000");
    premiums.put("999N003V01", "42000");
    premiums.put("999N004V01", "43000");
    List<RecommendedOption> options =
        rank(request("999N004V01", "999N003V01", "999N002V01", "999N001V01")).options();
    assertThat(uins(options)).containsExactly("999N001V01", "999N002V01", "999N003V01");
    assertThat(options).extracting(RecommendedOption::getRank).containsExactly(1, 2, 3);
  }

  @Test
  void aWithheldPremiumIsNeverQuoted() {
    premiums.put("999N001V01", "40000");
    for (RankingRequest request :
        List.of(
            request("999N001V01").tobacco12m(null),
            request("999N001V01").flags(List.of("MEDICAL_UW", "PREMIUM_WITHHELD")))) {
      List<RecommendedOption> options = rank(request).options();
      assertThat(options).singleElement().extracting(RecommendedOption::getQuote).isNull();
      assertThat(options.get(0).getReasonCodes()).contains("PREMIUM_WITHHELD");
    }
    assertThat(priced).isEmpty();
  }

  @Test
  void anUnratedCandidateStaysWithoutAQuote() {
    premiums.put("999N002V01", "40000");
    List<RecommendedOption> options = rank(request("999N001V01", "999N002V01")).options();
    assertThat(uins(options)).containsExactly("999N002V01", "999N001V01");
    assertThat(options.get(1).getQuote()).isNull();
    assertThat(options.get(1).getReasonCodes()).contains("RATING_UNAVAILABLE");
  }

  @Test
  void coverAndTermAreFittedToTheProduct() {
    premiums.put("999N001V01", "10000");
    // ₹10 lakh needed, the minimum is ₹25 lakh; 5 years needed, the minimum is 10.
    RankingRequest small = request("999N001V01");
    small.getSuitability().recommendedCoverInr("1000000").termYears(5);
    RecommendedOption option = rank(small).options().getFirst();
    assertThat(option.getSumAssuredInr()).isEqualTo("2500000");
    assertThat(option.getTermYears()).isEqualTo(10);
    assertThat(option.getProtectionGapInr()).isEqualTo("0");
    assertThat(option.getReasonCodes()).contains("RANK-SA-MIN", "RANK-TERM-MIN");

    // 60 years old wanting 30 years: maturity 85 allows 25.
    RankingRequest old = request("999N001V01").ageYears(60);
    old.getSuitability().termYears(30);
    option = rank(old).options().getFirst();
    assertThat(option.getTermYears()).isEqualTo(25);
    assertThat(option.getReasonCodes()).contains("RANK-TERM-CAPPED");

    // Outside the entry band: not a candidate.
    assertThat(rank(request("999N001V01").ageYears(70)).options()).isEmpty();
  }

  @Test
  void ridersFollowTheRiderFitRules() {
    premiums.put("999N001V01", "40000");
    // 55: critical illness no longer fits (age ≤ 50), the dependant riders still do.
    assertThat(rank(request("999N001V01").ageYears(55)).options().getFirst().getRiderUins())
        .containsExactly(ADB, WOP);
    // No dependants: only critical illness.
    RankingRequest single = request("999N001V01");
    single.getSuitability().getAssumptions().setDependencyYears(0);
    assertThat(rank(single).options().getFirst().getRiderUins()).containsExactly(CI);
    // Amber affordability: no rider fits.
    RankingRequest amber = request("999N001V01");
    amber.getSuitability().setAffordability(AffordabilityEnum.AMBER);
    assertThat(rank(amber).options().getFirst().getRiderUins()).isEmpty();
    // A rider whose cover limit is below the option's cover isn't attached.
    products.get("999N001V01").getRiders().getFirst().setSaMaxInr("5000000");
    assertThat(rank(request("999N001V01")).options().getFirst().getRiderUins())
        .containsExactly(WOP, CI);
  }

  @Test
  void onlyAFitWithCoverIsRanked() {
    premiums.put("999N001V01", "40000");
    RankingRequest noGap = request("999N001V01");
    noGap.getSuitability().outcome(OutcomeEnum.NO_GAP);
    assertThat(rank(noGap).options()).isEmpty();
    RankingRequest noCover = request("999N001V01");
    noCover.getSuitability().recommendedCoverInr("0");
    assertThat(rank(noCover).options()).isEmpty();
  }

  @Test
  void everyBuiltQuotePassesTheAdaptersChecksAndRankingIsRepeatable() {
    premiums.put("999N001V01", "40000");
    premiums.put("999N005V01", "50000");
    RankingRequest request = request("999N001V01", "999N005V01");
    List<RecommendedOption> first = rank(request).options();
    assertThat(rank(request).options()).isEqualTo(first);
    assertThat(priced).hasSize(4);
    for (QuoteRequest q : priced) {
      QuoteAdapter.validate(products.get(q.getUin()), q, STEP); // throws if out of bounds
      assertThat(q.getPpt()).isEqualTo(PptOption.REGULAR);
      assertThat(q.getPins().getRules()).isEqualTo(RULES);
    }
  }

  @Test
  void weightsMustCoverEveryRulesVersionAndSumToOne(@TempDir Path dir) throws Exception {
    assertThat(SHIPPED.rankerVersion()).isEqualTo("ranker-2026.09.1");
    assertThat(SHIPPED.owner()).isEqualTo("compliance");
    String shipped = Files.readString(WEIGHTS.resolve("weights-2026.09.1.yaml"));
    Files.writeString(dir.resolve("w.yaml"), shipped);
    assertThatThrownBy(
            () -> Ranker.load("file:" + dir + "/*.yaml", List.of(RULES, "rules-2026.10.1")))
        .hasMessageContaining("every rules version needs one ranking weights file");
    Files.writeString(
        dir.resolve("w.yaml"), shipped.replace("rider_fit: \"0.10\"", "rider_fit: \"0.20\""));
    assertThatThrownBy(() -> Ranker.load("file:" + dir + "/*.yaml", List.of(RULES)))
        .hasMessageContaining("sum to 1");
  }

  // --- helpers -----------------------------------------------------------------------------

  private Ranked rank(RankingRequest request) {
    return Ranker.rank(request, candidates(), SHIPPED, STEP, this::quote);
  }

  private Optional<PremiumQuote> quote(Product product, QuoteRequest request) {
    priced.add(request);
    return Optional.ofNullable(premiums.get(product.getUin()))
        .map(p -> new PremiumQuote().uin(product.getUin()).annualPremiumInr(p));
  }

  private List<Candidate> candidates() {
    return products.values().stream()
        .sorted((a, b) -> a.getUin().compareTo(b.getUin()))
        .map(p -> new Candidate(p, new QuoteDefaults(PptOption.REGULAR, Frequency.ANNUAL)))
        .toList();
  }

  /** The TDD example after S2: 34, ₹3.75 crore for 26 years, green, fit types TERM and ROP. */
  private static RankingRequest request(String... eligible) {
    SuitabilityResult suitability =
        new SuitabilityResult(
            null,
            OutcomeEnum.FIT,
            new BigDecimal("0.975"),
            List.of(ProductType.TERM, ProductType.TERM_ROP),
            Map.of(),
            "37147714.43",
            "37500000",
            "60000000",
            26,
            AffordabilityEnum.GREEN,
            List.of(),
            new SuitabilityAssumptions(60, 26, "0.07", "0.05", "0.30", "200000", "750000"),
            List.of(),
            List.of(),
            "actuarial-2026.09.1",
            RULES,
            "a".repeat(64));
    return new RankingRequest(
        new Pins(RULES),
        new ArrayList<>(Arrays.asList(eligible)),
        suitability,
        new ArrayList<>(),
        "web",
        "en-IN",
        OffsetDateTime.now(ZoneOffset.UTC),
        false,
        34,
        new ArrayList<>());
  }

  private static Product product(String uin, ProductType type, String saMax, String... riders) {
    List<Rider> attached =
        Arrays.stream(riders)
            .map(r -> new Rider(r, "rider " + r, List.of(uin), null, true))
            .toList();
    return new Product(
        uin,
        "product " + uin,
        type,
        ProductStatus.IN_FORCE,
        18,
        65,
        85,
        "2500000",
        saMax,
        10,
        40,
        List.of(PptOption.REGULAR, PptOption.LIMITED_10),
        List.of("lumpsum"),
        List.of(riders),
        LocalDate.of(2026, 9, 1),
        null,
        true,
        true,
        attached,
        List.of());
  }

  private static List<String> uins(List<RecommendedOption> options) {
    return options.stream().map(RecommendedOption::getUin).toList();
  }

  private static Weights load() {
    try {
      return Ranker.load("file:" + WEIGHTS + "/*.yaml", List.of(RULES)).get(RULES);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
