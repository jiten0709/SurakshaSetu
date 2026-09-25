package com.surakshasetu.domain.common;

import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.ProductType;
import org.springframework.context.annotation.Configuration;
import org.springframework.format.FormatterRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Query parameters carry the contract's enum values (in_force), not the Java constant names
 * (IN_FORCE), so they bind through the generated fromValue. An unknown value is a 400.
 */
@Configuration
class ContractEnumConverters implements WebMvcConfigurer {

  @Override
  public void addFormatters(FormatterRegistry registry) {
    registry.addConverter(String.class, ProductStatus.class, ProductStatus::fromValue);
    registry.addConverter(String.class, ProductType.class, ProductType::fromValue);
  }
}
