package com.example.blog.dto;

import com.example.blog.entity.User;

public class UserDto {
    public Long id;
    public String email;
    public String name;

    public static UserDto from(User user) {
        UserDto dto = new UserDto();
        dto.id = user.getId();
        dto.email = user.getEmail();
        dto.name = user.getName();
        return dto;
    }
}
