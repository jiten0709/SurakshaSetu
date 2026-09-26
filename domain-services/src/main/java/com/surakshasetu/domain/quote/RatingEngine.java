package com.surakshasetu.domain.quote;

import com.surakshasetu.domain.contract.model.Gender;
import com.surakshasetu.domain.contract.model.PptOption;
import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.jspecify.annotations.Nullable;

/**
 * The rating port: indicative premiums come only from here (TDD §3.8). The quote adapter prices
 * quotes, ranking options and alternatives through it; suitability prices its affordability
 * estimate; eligibility asks whether a product rates by gender.
 */
public interface RatingEngine {

  /** What gets rated. The quote adapter has already checked it against the product's limits. */
  record Basis(
      String uin,
      int age,
      boolean tobacco,
      @Nullable Gender gender,
      BigDecimal sumAssured,
      int termYears,
      PptOption ppt,
      List<String> riderUins) {}

  /** GST-inclusive annual premiums: {@code total} is the base plus every rider. */
  record Premium(BigDecimal total, Map<String, BigDecimal> riders) {}

  /** The rate table's version, reported on every quote. */
  String version();

  /** Whether the product's rates differ by gender, so S1 must ask for it. */
  boolean ratesByGender(String uin);

  /**
   * The premium, or empty when this engine has no rate for the basis: an unknown product or rider,
   * an age or term outside its bands, or a gender-rated product without a gender.
   */
  Optional<Premium> rate(Basis basis);
}
