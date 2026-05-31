package com.example.blog.entity

import jakarta.persistence.Column
import jakarta.persistence.Entity
import jakarta.persistence.GeneratedValue
import jakarta.persistence.Id
import jakarta.persistence.JoinColumn
import jakarta.persistence.ManyToOne
import jakarta.persistence.Table

@Entity
@Table(name = "posts")
class Post {
    @Id
    @GeneratedValue
    var id: Long? = null

    @ManyToOne
    @JoinColumn(name = "author_id")
    var author: User? = null

    @Column
    var title: String = ""

    @Column(columnDefinition = "TEXT")
    var body: String = ""
}
