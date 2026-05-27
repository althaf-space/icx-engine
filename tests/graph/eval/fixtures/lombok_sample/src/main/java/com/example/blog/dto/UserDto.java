package com.example.blog.dto;

import lombok.Data;
import lombok.Builder;

@Data
@Builder
public class UserDto {
    private Long id;
    private String email;
    private String name;
}
