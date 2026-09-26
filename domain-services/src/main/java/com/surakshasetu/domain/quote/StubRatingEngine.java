package com.surakshasetu.domain.quote;

import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.contract.model.Gender;
import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.Resource;
import org.springframework.stereotype.Component;

/**
 * DUMMY rating over a rate table ({@code resources/rating/*.yaml}; the file's header gives the
 * formula). It stands in for the core rating engine phase 1 doesn't have. The GST rate is a table
 * parameter, never code.
 */
@Component
class StubRatingEngine implements RatingEngine {

  private static final Logger log = LoggerFactory.getLogger(StubRatingEngine.class);
  private static final BigDecimal MILLE = new BigDecimal("1000");

  record Table(
      String ratingVersion,
      boolean isDummy,
      BigDecimal gstRate,
      Map<String, ProductRates> products,
      Map<String, BigDecimal> riders) {}

  record ProductRates(
      BigDecimal tobaccoLoading,
      Map<String, BigDecimal> pptLoading,
      Map<String, BigDecimal> genderLoading,
      List<Cell> baseRates) {}

  /** A base rate per ₹1,000 for an inclusive age band and term band. */
  record Cell(int ageMin, int ageMax, int termMin, int termMax, BigDecimal rate) {}

  private final Table table;

  StubRatingEngine(@Value("${surakshasetu.rating.table}") Resource table) throws IOException {
    this.table = Rules.YAML.readValue(table.getContentAsByteArray(), Table.class);
    log.info("rate table loaded: {}", this.table.ratingVersion());
  }

  @Override
  public String version() {
    return table.ratingVersion();
  }

  @Override
  public boolean ratesByGender(String uin) {
    ProductRates rates = table.products().get(uin);
    return rates != null && !rates.genderLoading().isEmpty();
  }

  @Override
  public Optional<Premium> rate(Basis basis) {
    ProductRates rates = table.products().get(basis.uin());
    if (rates == null) {
      return Optional.empty();
    }
    BigDecimal ppt = rates.pptLoading().get(basis.ppt().getValue());
    Cell cell =
        rates.baseRates().stream()
            .filter(c -> c.ageMin() <= basis.age() && basis.age() <= c.ageMax())
            .filter(c -> c.termMin() <= basis.termYears() && basis.termYears() <= c.termMax())
            .findFirst()
            .orElse(null);
    BigDecimal gender = genderLoading(rates, basis.gender());
    if (ppt == null || cell == null || gender == null) {
      return Optional.empty();
    }
    BigDecimal perMille = cell.rate().multiply(ppt).multiply(gender);
    if (basis.tobacco()) {
      perMille = perMille.multiply(rates.tobaccoLoading());
    }
    BigDecimal total = withGst(basis.sumAssured(), perMille);
    Map<String, BigDecimal> riders = new LinkedHashMap<>();
    for (String rider : basis.riderUins()) {
      BigDecimal rate = table.riders().get(rider);
      if (rate == null) {
        return Optional.empty();
      }
      BigDecimal premium = withGst(basis.sumAssured(), rate.multiply(ppt));
      riders.put(rider, premium);
      total = total.add(premium);
    }
    return Optional.of(new Premium(total, riders));
  }

  /** 1 when the product isn't rated by gender; null when it is and the gender is missing. */
  private static @Nullable BigDecimal genderLoading(ProductRates rates, @Nullable Gender gender) {
    if (rates.genderLoading().isEmpty()) {
      return BigDecimal.ONE;
    }
    return gender == null ? null : rates.genderLoading().get(gender.getValue());
  }

  /** SA / 1,000 × the rate, plus GST, to the paisa. */
  private BigDecimal withGst(BigDecimal sumAssured, BigDecimal perMille) {
    return sumAssured
        .multiply(perMille)
        .multiply(BigDecimal.ONE.add(table.gstRate()))
        .divide(MILLE, 2, RoundingMode.HALF_UP);
  }
}
