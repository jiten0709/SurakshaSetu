package com.surakshasetu.domain.ranking;

import static com.surakshasetu.domain.quote.QuoteAdapter.money;

import com.surakshasetu.domain.catalog.CatalogRepository.QuoteDefaults;
import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.contract.model.PremiumQuote;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.QuoteRequest;
import com.surakshasetu.domain.contract.model.RankingRequest;
import com.surakshasetu.domain.contract.model.RecommendedOption;
import com.surakshasetu.domain.contract.model.Rider;
import com.surakshasetu.domain.contract.model.SuitabilityResult;
import com.surakshasetu.domain.contract.model.SuitabilityResult.OutcomeEnum;
import com.surakshasetu.domain.quote.QuoteAdapter;
import com.surakshasetu.domain.quote.QuoteAdapter.Sizing;
import java.io.IOException;
import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.BiFunction;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.stereotype.Component;

/**
 * The deterministic ranker (TDD §3.8). Candidates are the eligible, fitting products on sale, less
 * the excluded ones; each is sized to the recommended cover, priced through the quote adapter and
 * scored with the compliance-owned weights of the pinned rules version. The inputs are needs,
 * cover, term and premiums only: commission, margin and incentives are never inputs, and {@code
 * CommercialFieldBanTest} fails the build if one appears.
 */
@Component
public class Ranker {

  private static final Logger log = LoggerFactory.getLogger(Ranker.class);
  private static final MathContext MC = MathContext.DECIMAL128;
  private static final int TOP = 3;

  /** One {@code ranking/weights-*.yaml}, bound to a rules version. */
  public record Weights(
      String rankerVersion,
      String rulesVersion,
      String owner,
      boolean isDummy,
      ScoreWeights weights,
      List<RiderRule> riderRules) {}

  public record ScoreWeights(
      BigDecimal needCoverage, BigDecimal affordability, BigDecimal riderFit) {}

  /** The rider fits the customer when every non-null condition holds. */
  public record RiderRule(
      String riderUin,
      String reasonCode,
      @Nullable Integer dependencyYearsMin,
      @Nullable Integer ageMax,
      List<String> affordability) {}

  /** A product on sale on the request's date, with its quote defaults. */
  public record Candidate(Product product, QuoteDefaults defaults) {}

  /** The top options, and how many candidates were scored. */
  public record Ranked(List<RecommendedOption> options, int candidates) {}

  /** An option before scoring, with its need-coverage and rider-fit parts. */
  private record Built(RecommendedOption option, BigDecimal need, BigDecimal riderFit) {}

  private record Scored(RecommendedOption option, BigDecimal score) {}

  private final Map<String, Weights> byRules;

  Ranker(Rules rules) throws IOException {
    this.byRules = load("classpath*:ranking/weights-*.yaml", rules.active());
    log.info("ranking weights loaded: {}", byRules.keySet());
  }

  /**
   * Every weights file the pattern matches, by rules version. Each loaded rules version needs
   * exactly one, and its weights must be non-negative and sum to 1.
   */
  static Map<String, Weights> load(String pattern, Collection<String> rulesVersions)
      throws IOException {
    Map<String, Weights> byRules = new HashMap<>();
    for (Resource file : new PathMatchingResourcePatternResolver().getResources(pattern)) {
      Weights w = Rules.YAML.readValue(file.getContentAsByteArray(), Weights.class);
      ScoreWeights s = w.weights();
      List<BigDecimal> parts = List.of(s.needCoverage(), s.affordability(), s.riderFit());
      if (parts.stream().anyMatch(x -> x.signum() < 0)
          || parts.stream().reduce(BigDecimal.ZERO, BigDecimal::add).compareTo(BigDecimal.ONE)
              != 0) {
        throw new IllegalStateException(
            "ranking weights " + w.rankerVersion() + " must be non-negative and sum to 1");
      }
      if (byRules.put(w.rulesVersion(), w) != null) {
        throw new IllegalStateException("two ranking weights files for " + w.rulesVersion());
      }
    }
    if (!byRules.keySet().equals(Set.copyOf(rulesVersions))) {
      throw new IllegalStateException(
          "every rules version needs one ranking weights file: rules "
              + rulesVersions
              + ", weights "
              + byRules.keySet());
    }
    return byRules;
  }

  /** The weights of a loaded rules version. */
  public Weights weights(String rulesVersion) {
    return byRules.get(rulesVersion);
  }

  /**
   * Ranks the candidates; pure apart from {@code quote}. No options when the suitability outcome
   * isn't FIT or there is no cover to recommend. A withheld premium (tobacco undisclosed, or the
   * PREMIUM_WITHHELD flag) or a rating the engine can't give leaves the option without a quote.
   */
  public static Ranked rank(
      RankingRequest request,
      List<Candidate> onSale,
      Weights weights,
      BigDecimal step,
      BiFunction<Product, QuoteRequest, Optional<PremiumQuote>> quote) {
    SuitabilityResult suitability = request.getSuitability();
    BigDecimal recommended = new BigDecimal(suitability.getRecommendedCoverInr());
    if (suitability.getOutcome() != OutcomeEnum.FIT || recommended.signum() <= 0) {
      return new Ranked(List.of(), 0);
    }
    int age = request.getAgeYears();
    int neededTerm = suitability.getTermYears();
    List<RiderRule> fitting =
        weights.riderRules().stream()
            .filter(
                rule ->
                    fits(
                        rule,
                        age,
                        suitability.getAssumptions().getDependencyYears(),
                        suitability.getAffordability().getValue()))
            .toList();
    boolean withheld =
        request.getTobacco12m() == null || request.getFlags().contains("PREMIUM_WITHHELD");

    List<Built> built = new ArrayList<>();
    for (Candidate candidate : onSale) {
      Product p = candidate.product();
      if (!request.getEligibleUins().contains(p.getUin())
          || request.getExcludedUins().contains(p.getUin())
          || !suitability.getFitTypes().contains(p.getCategory())) {
        continue;
      }
      Optional<Sizing> sizing = QuoteAdapter.size(p, age, recommended, neededTerm, step);
      if (sizing.isEmpty()) {
        continue; // the product doesn't take this age
      }
      BigDecimal sa = sizing.get().sumAssured();
      int term = sizing.get().termYears();

      List<String> reasons = new ArrayList<>();
      reasons.add("RANK-FIT-" + p.getCategory().getValue());
      int cover = sa.compareTo(recommended);
      if (cover != 0) {
        reasons.add(cover < 0 ? "RANK-SA-CAPPED" : "RANK-SA-MIN");
      }
      if (term != neededTerm) {
        reasons.add(term < neededTerm ? "RANK-TERM-CAPPED" : "RANK-TERM-MIN");
      }
      List<String> riders = new ArrayList<>();
      for (RiderRule rule : fitting) {
        Rider rider = QuoteAdapter.attachable(p, rule.riderUin());
        if (rider != null
            && (rider.getSaMaxInr() == null
                || sa.compareTo(new BigDecimal(rider.getSaMaxInr())) <= 0)) {
          riders.add(rule.riderUin());
          reasons.add(rule.reasonCode());
        }
      }

      QuoteRequest q =
          new QuoteRequest(
                  request.getPins(),
                  p.getUin(),
                  money(sa),
                  term,
                  candidate.defaults().ppt(),
                  List.copyOf(riders),
                  age,
                  request.getTobacco12m(),
                  candidate.defaults().frequency())
              .gender(request.getGender());
      PremiumQuote premium = null;
      if (withheld) {
        reasons.add("PREMIUM_WITHHELD");
      } else {
        premium = quote.apply(p, q).orElse(null);
        if (premium == null) {
          reasons.add("RATING_UNAVAILABLE");
        }
      }

      BigDecimal need =
          ratio(sa, recommended)
              .multiply(
                  neededTerm <= 0
                      ? BigDecimal.ONE
                      : ratio(BigDecimal.valueOf(term), BigDecimal.valueOf(neededTerm)));
      BigDecimal riderFit =
          fitting.isEmpty()
              ? BigDecimal.ONE
              : BigDecimal.valueOf(riders.size()).divide(BigDecimal.valueOf(fitting.size()), MC);
      RecommendedOption option =
          new RecommendedOption(
                  0,
                  p.getUin(),
                  money(sa),
                  term,
                  QuoteAdapter.pptYears(candidate.defaults().ppt(), term),
                  premium,
                  reasons)
              .riderUins(List.copyOf(riders))
              .protectionGapInr(money(recommended.subtract(sa).max(BigDecimal.ZERO)));
      built.add(new Built(option, need, riderFit));
    }

    BigDecimal cheapest =
        built.stream()
            .map(b -> premium(b.option()))
            .filter(x -> x != null)
            .min(Comparator.naturalOrder())
            .orElse(null);
    ScoreWeights w = weights.weights();
    List<Scored> scored = new ArrayList<>();
    for (Built b : built) {
      BigDecimal premium = premium(b.option());
      BigDecimal affordability =
          premium == null || premium.signum() == 0 || cheapest == null
              ? BigDecimal.ZERO
              : cheapest.divide(premium, MC);
      BigDecimal score =
          w.needCoverage()
              .multiply(b.need())
              .add(w.affordability().multiply(affordability))
              .add(w.riderFit().multiply(b.riderFit()))
              .setScale(6, RoundingMode.HALF_UP);
      log.debug("candidate {} scored {}", b.option().getUin(), score);
      scored.add(new Scored(b.option(), score));
    }

    List<RecommendedOption> options =
        scored.stream()
            .sorted(
                Comparator.comparing(Scored::score)
                    .reversed()
                    .thenComparing(
                        s -> premium(s.option()), Comparator.nullsLast(Comparator.naturalOrder()))
                    .thenComparing(s -> s.option().getUin()))
            .limit(TOP)
            .map(Scored::option)
            .toList();
    for (int i = 0; i < options.size(); i++) {
      options.get(i).setRank(i + 1);
    }
    return new Ranked(options, built.size());
  }

  private static @Nullable BigDecimal premium(RecommendedOption option) {
    return option.getQuote() == null
        ? null
        : new BigDecimal(option.getQuote().getAnnualPremiumInr());
  }

  private static boolean fits(RiderRule rule, int age, int dependencyYears, String affordability) {
    return (rule.dependencyYearsMin() == null || dependencyYears >= rule.dependencyYearsMin())
        && (rule.ageMax() == null || age <= rule.ageMax())
        && rule.affordability().contains(affordability);
  }

  /** min(a / b, 1). */
  private static BigDecimal ratio(BigDecimal a, BigDecimal b) {
    return a.divide(b, MC).min(BigDecimal.ONE);
  }
}
